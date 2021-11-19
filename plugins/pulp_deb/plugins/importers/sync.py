import logging
import os
import urlparse
import hashlib
import gnupg
from collections import defaultdict
from gettext import gettext as _
from distutils.version import LooseVersion

from debpkgr import aptrepo
from nectar.request import DownloadRequest
from pulp.plugins.util import misc, publish_step
from pulp.common.error_codes import Error
from pulp.server.exceptions import PulpCodedTaskFailedException

from pulp_deb.common import constants, ids
from pulp_deb.plugins.db import models

_logger = logging.getLogger(__name__)


DEBSYNC001 = Error(
    "DEBSYNC001",
    "Unable to sync %(repo_id)s from %(feed_url)s:"
    " expected one comp, got %(comp_count)s",
    ["repo_id", "feed_url", "comp_count"])

DEBSYNC002 = Error(
    "DEBSYNC002",
    "Unable to sync %(repo_id)s from %(feed_url)s: mismatching checksums"
    " for %(filename)s: expected %(checksum_expected)s,"
    " actual %(checksum_actual)s",
    ["repo_id", "feed_url", "filename", "checksum_expected", "checksum_actual"])


def verify_unit_callback(unit):
    if not os.path.isfile(unit.storage_path):
        return False
    checksums = unit.calculate_deb_checksums(unit.storage_path)
    if unit.sha1 and unit.sha1 != checksums['sha1']:
        return False
    if unit.md5sum and unit.md5sum != checksums['md5sum']:
        return False
    if unit.sha256 and unit.sha256 != checksums['sha256']:
        return False
    return True


class RepoSync(publish_step.PluginStep):
    Type_Class_Map = {
        models.DebPackage.TYPE_ID: models.DebPackage,
    }

    def __init__(self, repo, conduit, config):
        """
        :param repo:        repository to sync
        :type  repo:        pulp.plugins.model.Repository
        :param conduit:     sync conduit to use
        :type  conduit:     pulp.plugins.conduits.repo_sync.RepoSyncConduit
        :param config:      config object for the sync
        :type  config:      pulp.plugins.config.PluginCallConfiguration
        """
        super(RepoSync, self).__init__(step_type=constants.SYNC_STEP,
                                       repo=repo,
                                       conduit=conduit,
                                       config=config)
        self.description = _('Syncing Repository')

        self.feed_url = self.get_config().get('feed').strip('/')
        self.distributions = self.get_config().get('releases', 'stable').split(',')
        self.architectures = split_or_none(self.get_config().get('architectures'))
        self.components = split_or_none(self.get_config().get('components'))
        self.remove_missing = self.get_config().get_boolean(
            constants.CONFIG_REMOVE_MISSING_UNITS, constants.CONFIG_REMOVE_MISSING_UNITS_DEFAULT)
        self.repair_sync = self.get_config().get_boolean(constants.CONFIG_REPAIR_SYNC, False)

        self.unit_relative_urls = {}
        self.available_units = None
        # dicts with the distribution as keys to multiplex variables
        self.apt_repo_meta = {}
        self.release_units = {}
        self.release_files = {
            distribution: os.path.join(self.get_working_dir(), distribution, 'Release')
            for distribution in self.distributions
        }
        self.feed_urls = {
            distribution: urlparse.urljoin(self.feed_url + '/', '/'.join(['dists', distribution]))
            for distribution in self.distributions
        }
        self.release_urls = {
            distribution: urlparse.urljoin(self.feed_urls[distribution] + '/', 'Release')
            for distribution in self.distributions
        }
        self.packages_urls = {}
        # double dicts with distribution/component as keys
        self.component_units = defaultdict(dict)
        self.component_packages = defaultdict(dict)

        for distribution in self.distributions:
            misc.mkdir(os.path.dirname(self.release_files[distribution]))
            _logger.info("Downloading %s", self.release_urls[distribution])

        # defining lifecycle
        #  metadata
        self.add_child(publish_step.DownloadStep(
            constants.SYNC_STEP_RELEASE_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving metadata: Release file(s)'),
            downloads=[
                DownloadRequest(self.release_urls[distribution], self.release_files[distribution])
                for distribution in self.distributions] + [
                DownloadRequest(self.release_urls[distribution] + '.gpg',
                                self.release_files[distribution] + '.gpg')
                for distribution in self.distributions]
        ))

        self.add_child(ParseReleaseStep(constants.SYNC_STEP_RELEASE_PARSE))

        self.step_download_Packages = publish_step.DownloadStep(
            constants.SYNC_STEP_PACKAGES_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving metadata: Packages files'))
        self.add_child(self.step_download_Packages)

        self.add_child(ParsePackagesStep(constants.SYNC_STEP_PACKAGES_PARSE))

        #  packages
        if self.repair_sync:
            verify_unit = verify_unit_callback
        else:
            verify_unit = None
        self.step_local_units = publish_step.GetLocalUnitsStep(
            importer_type=ids.TYPE_ID_IMPORTER, verify_unit=verify_unit)
        self.add_child(self.step_local_units)

        self.add_child(CreateRequestsUnitsToDownload(
            constants.SYNC_STEP_UNITS_DOWNLOAD_REQUESTS))

        self.step_download_units = publish_step.DownloadStep(
            constants.SYNC_STEP_UNITS_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving units'))
        self.add_child(self.step_download_units)

        self.add_child(SaveDownloadedUnits(constants.SYNC_STEP_SAVE))

        #  metadata
        self.add_child(SaveMetadataStep(constants.SYNC_STEP_SAVE_META))

        self.debs_to_check = {}
        self.deb_comps_to_check = {}
        self.deb_releases_to_check = {}
        # cleanup
        if self.remove_missing:
            units_to_check = self.conduit.get_units()
            self.debs_to_check = {unit.id: unit for unit in units_to_check
                                  if unit.type_id == ids.TYPE_ID_DEB}
            self.deb_comps_to_check = {unit.id: unit for unit in units_to_check
                                       if unit.type_id == ids.TYPE_ID_DEB_COMP}
            self.deb_releases_to_check = {unit.id: unit for unit in units_to_check
                                          if unit.type_id == ids.TYPE_ID_DEB_RELEASE}
            del units_to_check
            self.add_child(OrphanRemovedUnits(constants.SYNC_STEP_ORPHAN_REMOVED_UNITS))


class ParseReleaseStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(ParseReleaseStep, self).__init__(*args, **kwargs)
        self.description = _('Parse Release Files')

    def gnupg_factory(self, *args, **kwargs):
        if 'homedir' in kwargs.keys():
            module_version_gnupg = LooseVersion(gnupg.__version__)
            if module_version_gnupg < LooseVersion('1.0.0'):
                kwargs['gnupghome'] = kwargs['homedir']
                del(kwargs['homedir'])
        return gnupg.GPG(*args, **kwargs)

    def verify_release_file(self, distribution):
        rel_file = self.parent.release_files[distribution]
        # check if Release file exists
        if not os.path.isfile(rel_file):
            raise Exception("Release file not found. Check the feed option.")
        # check signature
        if not self.get_config().get_boolean(constants.CONFIG_REQUIRE_SIGNATURE, False):
            return
        worker_gpg_homedir = os.path.join(self.get_working_dir(), 'gpg-home')
        if not os.path.exists(worker_gpg_homedir):
            os.mkdir(worker_gpg_homedir, 0o700)
        gpg = self.gnupg_factory(homedir=worker_gpg_homedir)
        shared_gpg = self.gnupg_factory(homedir=os.path.join('/', 'var', 'lib', 'pulp', 'gpg-home'))

        if self.get_config().get(constants.CONFIG_GPG_KEYS):
            import_res = gpg.import_keys(self.get_config().get(constants.CONFIG_GPG_KEYS))
            _logger.info("Importing GPG-Key: %r", import_res.results)
            if import_res.count == 0:
                raise Exception("GPG-Key not imported: %r" % import_res.results)

        if self.get_config().get(constants.CONFIG_ALLOWED_KEYS):
            keyserver = self.get_config().get(constants.CONFIG_KEYSERVER,
                                              constants.CONFIG_KEYSERVER_DEFAULT)

            fingerprints = split_or_none(self.get_config().get(constants.CONFIG_ALLOWED_KEYS)) or []
            # TODO check if full fingerprints are provided
            for fingerprint in fingerprints:
                # remove spaces from fingerprint (space would mark the next key)
                fingerprint = fingerprint.replace(' ', '')
                if fingerprint not in [
                        key['fingerprint'] for key in shared_gpg.list_keys()]:
                    shared_gpg.recv_keys(keyserver, fingerprint)
            gpg.import_keys(shared_gpg.export_keys(fingerprints))

        if len(gpg.list_keys()) == 0:
            raise Exception("No GPG-keys in keyring, did the import fail?")

        if not os.path.isfile(rel_file + '.gpg'):
            raise Exception("Release.gpg not found. Could not verify Release file integrity.")

        if LooseVersion(gnupg.__version__) < LooseVersion('1.0.0'):
            with open(rel_file + '.gpg') as f:
                verified = gpg.verify_file(f, rel_file)
                if not verified.valid:
                    raise Exception("Release file verification failed! {}".format(verified.stderr))
        else:
            with open(rel_file) as f:
                verified = gpg.verify_file(f, rel_file + '.gpg')
                if not verified.valid:
                    raise Exception("Release file verification failed! {}".format(verified.stderr))

    def process_main(self, item=None):
        distributions = self.parent.distributions
        components = self.parent.components
        architectures = self.parent.architectures
        dl_reqs = []
        for distribution in distributions:
            self.verify_release_file(distribution)
            # generate repo_metas for each distribution
            self.parent.apt_repo_meta[distribution] = repometa = aptrepo.AptRepoMeta(
                release=open(self.parent.release_files[distribution], "rb"),
                upstream_url=self.parent.feed_urls[distribution])
            # get release unit
            codename = repometa.codename
            suite = repometa.release.get('suite')
            release_unit = models.DebRelease.get_or_create_and_associate(
                repo=self.parent.repo,
                distribution=distribution,
                codename=codename,
                suite=suite,
            )
            self.parent.release_units[distribution] = release_unit

            # Prevent this unit from being cleaned up
            self.parent.deb_releases_to_check.pop(release_unit.id, None)
            # get release component units
            for release_file_component in repometa.components:
                component = release_file_component.split('/')[-1]
                if components is None or component in components:
                    comp_unit = self.parent.component_units[distribution][component] = \
                        models.DebComponent.get_or_create_and_associate(self.parent.repo,
                                                                        release_unit,
                                                                        component)
                    self.parent.component_packages[distribution][component] = []
                    # Prevent this unit from being cleaned up
                    self.parent.deb_comps_to_check.pop(comp_unit.id, None)
            # generate download requests for all relevant packages files
            rel_dl_reqs = repometa.create_Packages_download_requests(
                self.get_working_dir())
            # Filter the rel_dl_reqs by selected components and architectures
            if components:
                rel_dl_reqs = [
                    dlr for dlr in rel_dl_reqs
                    if dlr.data['component'].split('/')[-1] in components]
            if architectures:
                rel_dl_reqs = [
                    dlr for dlr in rel_dl_reqs
                    if dlr.data['architecture'] in architectures]
            self.parent.packages_urls[distribution] = set([dlr.url for dlr in rel_dl_reqs])
            dl_reqs.extend(rel_dl_reqs)
        self.parent.step_download_Packages._downloads = [
            DownloadRequest(dlr.url, dlr.destination, data=dlr.data)
            for dlr in dl_reqs]


class ParsePackagesStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(ParsePackagesStep, self).__init__(*args, **kwargs)
        self.description = _('Parse Packages Files')

    def process_main(self, item=None):
        distributions = self.parent.distributions
        dl_reqs = self.parent.step_download_Packages.downloads
        units = {}
        for distribution in distributions:
            repometa = self.parent.apt_repo_meta[distribution]
            repometa.validate_component_arch_packages_downloads(
                [dlr for dlr in dl_reqs
                    if dlr.url in self.parent.packages_urls[distribution]])
            for ca in repometa.iter_component_arch_binaries():
                for pkg in ca.iter_packages():
                    try:
                        checksum = pkg['SHA256']
                        self.parent.unit_relative_urls[checksum] = pkg['Filename']
                        if checksum in units:
                            unit = units[checksum]
                        else:
                            unit = models.DebPackage.from_packages_paragraph(pkg)
                            units[checksum] = unit
                    except (KeyError, ValueError):
                        _logger.warning(_("Invalid package record found. {}").format(pkg))
                        continue
                    component = ca.component.split('/')[-1]
                    self.parent.component_packages[distribution][component].append(unit.unit_key)
        self.parent.available_units = units.values()


class CreateRequestsUnitsToDownload(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(CreateRequestsUnitsToDownload, self).__init__(*args, **kwargs)
        self.description = _('Prepare Package Download')

    def process_main(self, item=None):
        wdir = os.path.join(self.get_working_dir())
        reqs = []

        feed_url = self.parent.feed_url

        step_download_units = self.parent.step_download_units
        step_download_units.path_to_unit = dict()
        dirs_to_create = set()

        for unit in self.parent.step_local_units.units_to_download:
            url = os.path.join(feed_url, self.parent.unit_relative_urls[unit.checksum])
            filename = os.path.basename(url)
            dest_dir = os.path.join(wdir, "packages", generate_internal_storage_path(filename))
            dirs_to_create.add(dest_dir)
            dest = os.path.join(dest_dir, filename)
            reqs.append(DownloadRequest(url, dest))
            step_download_units.path_to_unit[dest] = unit

        for dest_dir in dirs_to_create:
            misc.mkdir(dest_dir)
        step_download_units._downloads = reqs


class SaveDownloadedUnits(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(SaveDownloadedUnits, self).__init__(*args, **kwargs)
        self.description = _('Save and associate downloaded units')

    def verify_checksum(self, calculated, from_metadata, path):
        if calculated != from_metadata:
            raise PulpCodedTaskFailedException(
                DEBSYNC002, repo_id=self.get_repo().repo_obj.repo_id,
                feed_url=self.parent.feed_url,
                filename=os.path.basename(path),
                checksum_expected=from_metadata,
                checksum_actual=calculated)

    def process_main(self, item=None):
        path_to_unit = self.parent.step_download_units.path_to_unit
        repo = self.get_repo().repo_obj
        for path, unit in path_to_unit.items():
            checksums = unit.calculate_deb_checksums(path)
            if unit.sha1:
                self.verify_checksum(checksums['sha1'], unit.sha1, path)
            else:
                unit.sha1 = checksums['sha1']
            if unit.md5sum:
                self.verify_checksum(checksums['md5sum'], unit.md5sum, path)
            else:
                unit.md5sum = checksums['md5sum']
            if unit.sha256:
                self.verify_checksum(checksums['sha256'], unit.sha256, path)
            else:
                unit.sha256 = checksums['sha256']
            self.verify_checksum(unit.checksum, unit.sha256, path)
            unit.save_and_associate(path, repo, force=self.parent.repair_sync)


class SaveMetadataStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(SaveMetadataStep, self).__init__(*args, **kwargs)
        self.description = _('Save metadata')

    def process_main(self, item=None):
        for distribution in self.parent.distributions:
            for comp, comp_unit in self.parent.component_units[distribution].iteritems():
                # Start with an empty set if we want to delete old entries
                if self.parent.remove_missing:
                    comp_unit_packages_set = set()
                else:
                    comp_unit_packages_set = set(comp_unit.packages)
                for unit in [unit_key_to_unit(unit_key)
                             for unit_key in self.parent.component_packages[distribution][comp]]:
                    comp_unit_packages_set.add(unit.id)
                    # Prevent this unit from being cleaned up
                    self.parent.debs_to_check.pop(unit.id, None)
                comp_unit.packages = list(comp_unit_packages_set)
                comp_unit.save()


class OrphanRemovedUnits(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(OrphanRemovedUnits, self).__init__(*args, **kwargs)
        self.description = _('Orphan removed units')

    def process_main(self, item=None):
        for unit in self.parent.deb_releases_to_check.values():
            self.parent.conduit.remove_unit(unit)
        for unit in self.parent.deb_comps_to_check.values():
            self.parent.conduit.remove_unit(unit)
        for unit in self.parent.debs_to_check.values():
            self.parent.conduit.remove_unit(unit)


def unit_key_to_unit(unit_key):
    return models.DebPackage.objects.filter(**unit_key).first()


def generate_internal_storage_path(filename):
    """
    Generate the internal storage directory for a given deb filename

    :param filename: base filename of the unit
    :type filename: str

    :returns str: The relative directory path for storing the unit
    """
    hasher = hashlib.md5()
    hasher.update(filename)
    hash_digest = hasher.hexdigest()
    part1 = hash_digest[0:2]
    part2 = hash_digest[2:4]
    storage_path = os.path.join(part1, part2)
    return storage_path


def split_or_none(data):
    if data:
        return [x.strip() for x in data.split(',')]
    return None
