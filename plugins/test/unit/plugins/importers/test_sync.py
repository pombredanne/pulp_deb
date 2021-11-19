import os
import re

import mock
from argparse import Namespace
from pulp.common.plugins import importer_constants
from pulp.plugins.config import PluginCallConfiguration
from pulp.plugins.model import Repository as RepositoryModel
from pulp.server import exceptions

from .... import testbase

from pulp_deb.common import constants
from pulp_deb.common import ids
from pulp_deb.plugins.db import models
from pulp_deb.plugins.importers import sync


class _TestSyncBase(testbase.TestCase):
    remove_missing = False
    release = None
    repair_sync = False

    def setUp(self):
        super(_TestSyncBase, self).setUp()

        self.repo = RepositoryModel('repo1')
        self.conduit = mock.MagicMock()
        self.conduit.get_units.return_value = [
            Namespace(type_id=ids.TYPE_ID_DEB_RELEASE, id="AAAA"),
            Namespace(type_id=ids.TYPE_ID_DEB_COMP, id="BBBB"),
            Namespace(type_id=ids.TYPE_ID_DEB, id="CCCC"),
        ]
        plugin_config = {
            importer_constants.KEY_FEED: 'http://example.com/deb',
            constants.CONFIG_REQUIRE_SIGNATURE: False,
            importer_constants.KEY_UNITS_REMOVE_MISSING: self.remove_missing,
            constants.CONFIG_REPAIR_SYNC: self.repair_sync,
        }
        if self.release:
            plugin_config['releases'] = self.release
        else:
            self.release = 'stable'
        self.config = PluginCallConfiguration({}, plugin_config)
        self._task_current = mock.patch("pulp.server.managers.repo._common.task.current")
        obj = self._task_current.__enter__()
        obj.request.id = 'aabb'
        worker_name = "worker01"
        obj.request.configure_mock(hostname=worker_name)
        os.makedirs(os.path.join(self.pulp_working_dir, worker_name))
        self.step = sync.RepoSync(repo=self.repo,
                                  conduit=self.conduit,
                                  config=self.config)
        with open(self.step.release_files[self.release], "wb") as f:
            f.write("""\
Architectures: amd64
Components: main
SHA256:
 0000000000000000000000000000000000000000000000000000000000000001            67863 main/binary-amd64/Packages
 0000000000000000000000000000000000000000000000000000000000000003             9144 main/binary-amd64/Packages.bz2
 0000000000000000000000000000000000000000000000000000000000000002            14457 main/binary-amd64/Packages.gz
""")  # noqa
        with open(self.step.release_files[self.release] + '.gpg', "wb") as f:
            f.write("")

    def tearDown(self):
        self._task_current.__exit__()

    def test_init(self):
        self.assertEqual(self.step.step_id, constants.SYNC_STEP)

        # make sure the children are present
        step_ids = [child.step_id for child in self.step.children]
        expected_step_ids = [
            constants.SYNC_STEP_RELEASE_DOWNLOAD,
            constants.SYNC_STEP_RELEASE_PARSE,
            constants.SYNC_STEP_PACKAGES_DOWNLOAD,
            constants.SYNC_STEP_PACKAGES_PARSE,
            'get_local',
            constants.SYNC_STEP_UNITS_DOWNLOAD_REQUESTS,
            constants.SYNC_STEP_UNITS_DOWNLOAD,
            constants.SYNC_STEP_SAVE,
            constants.SYNC_STEP_SAVE_META,
        ]
        if self.remove_missing:
            expected_step_ids.append(constants.SYNC_STEP_ORPHAN_REMOVED_UNITS)
        self.assertEquals(expected_step_ids, step_ids)
        self.assertEquals(self.remove_missing, self.step.conduit.get_units.called)

    @mock.patch('pulp_deb.plugins.importers.sync.models.DebComponent')
    @mock.patch('pulp_deb.plugins.importers.sync.models.DebRelease')
    def test_ParseReleaseStep(self, _DebRelease, _DebComponent):
        step = self.step.children[1]
        self.assertEquals(constants.SYNC_STEP_RELEASE_PARSE, step.step_id)
        self.step.deb_releases_to_check = mock.MagicMock()
        self.step.deb_comps_to_check = mock.MagicMock()
        step.process_lifecycle()

        # Make sure we got a request for the best compression
        self.assertEquals(
            ['http://example.com/deb/dists/%s/main/binary-amd64/Packages.bz2' % self.release],
            [x.url for x in self.step.step_download_Packages.downloads])
        self.assertEquals(
            [os.path.join(
                self.pulp_working_dir,
                'worker01/aabb/dists/foo/main/binary-amd64/Packages.bz2')],
            [x.destination for x in self.step.step_download_Packages.downloads])
        # apt_repo_meta is set as a side-effect
        self.assertEquals(
            ['amd64'],
            self.step.apt_repo_meta[self.release].architectures)
        _DebRelease.get_or_create_and_associate.assert_called_once()
        _DebComponent.get_or_create_and_associate.assert_called_once()
        self.step.deb_releases_to_check.remove.assert_called_once()
        self.step.deb_comps_to_check.remove.assert_called_once()

    def _mock_repometa(self):
        repometa = self.step.apt_repo_meta[self.release] = mock.MagicMock(
            upstream_url="http://example.com/deb/dists/%s/" % self.release)

        pkgs = [
            dict(Package=x, Version="1-1", Architecture="amd64",
                 MD5sum="00{0}{0}".format(x),
                 SHA1="00{0}{0}".format(x),
                 SHA256="00{0}{0}".format(x),
                 Filename="pool/main/{0}_1-1_amd64.deb".format(x))
            for x in ["a", "b"]
        ]

        # Add a few invalid entries that should be ignored.
        invalid_pkgs = [
            dict(Homepage="http://1234"),
            dict(Package="c", Version="1.2-3", Architecture="amd64",
                 MD5sum="00cc", SHA1="00cc", SHA256="00cc"),
            dict(Package="c", Version="1.2-3",
                 MD5sum="00cc", SHA1="00cc", SHA256="00cc",
                 Filename="pool/main/c_1.2-3_amd64.deb"),
        ]

        comp_arch = mock.MagicMock(component='main', arch="amd64")
        comp_arch.iter_packages.return_value = pkgs + invalid_pkgs

        repometa.iter_component_arch_binaries.return_value = [comp_arch]
        return pkgs

    def test_ParsePackagesStep(self):
        pkgs = self._mock_repometa()
        dl1 = mock.MagicMock(destination="dest1")
        dl2 = mock.MagicMock(destination="dest2")
        self.step.packages_urls[self.release] = set(
            [u'http://example.com/deb/dists/%s/main/binary-amd64/Packages.bz2' % self.release])
        self.step.step_download_Packages._downloads = [dl1, dl2]
        self.step.component_packages[self.release]['main'] = []
        step = self.step.children[3]
        self.assertEquals(constants.SYNC_STEP_PACKAGES_PARSE, step.step_id)
        step.process_lifecycle()

        self.assertEquals(
            set([x['MD5sum'] for x in pkgs]),
            set([x.md5sum for x in self.step.available_units]))
        self.assertEquals(
            set([x['SHA1'] for x in pkgs]),
            set([x.sha1 for x in self.step.available_units]))
        self.assertEquals(
            set([x['SHA256'] for x in pkgs]),
            set([x.sha256 for x in self.step.available_units]))
        self.assertEquals(
            set([x['SHA256'] for x in pkgs]),
            set([x.checksum for x in self.step.available_units]))
        self.assertEquals(len(self.step.component_packages[self.release]['main']), 2)

    @mock.patch('os.path.isfile', return_value=True)
    @mock.patch('pulp.plugins.util.publish_step.repo_controller.associate_single_unit')
    @mock.patch('pulp.plugins.util.publish_step.units_controller.find_units')
    def test_getLocalUnits(self, _find_units, _associate_single_unit, _isfile):
        self.step.conduit.remove_unit = mock.MagicMock()
        pkgs = self._mock_repometa()
        units = [
            models.DebPackage.from_packages_paragraph(pkg)
            for pkg in pkgs
        ]
        if self.repair_sync:
            for unit in units:
                checksums = {
                    'md5sum': unit.md5sum,
                    'sha1': unit.sha1,
                    'sha256': unit.sha256,
                }
                unit.calculate_deb_checksums = mock.MagicMock(return_value=checksums)
            # Break the first one
            units[0].calculate_deb_checksums.return_value['sha1'] = "invalid"
        self.step.available_units = units
        _find_units.return_value = units
        step = self.step.children[4]
        step.process_lifecycle()
        if self.repair_sync:
            self.assertEqual(_associate_single_unit.call_count, len(units) - 1)
            self.assertEqual(len(step.units_to_download), 1)
            self.step.conduit.remove_unit.assert_not_called()
        else:
            self.assertEqual(_associate_single_unit.call_count, len(units))
            self.assertEqual(len(step.units_to_download), 0)
            self.step.conduit.remove_unit.assert_called_once()

    @mock.patch('pulp_deb.plugins.importers.sync.misc.mkdir')
    def test_CreateRequestsUnitsToDownload(self, _mkdir):
        pkgs = self._mock_repometa()
        units = [mock.MagicMock(md5sum=x['MD5sum'],
                                sha1=x['SHA1'],
                                sha256=x['SHA256'],
                                checksum=x['SHA256'],)
                 for x in pkgs]
        self.step.step_local_units.units_to_download = units
        self.step.unit_relative_urls = dict((p['SHA256'], p['Filename']) for p in pkgs)

        step = self.step.children[5]
        self.assertEquals(constants.SYNC_STEP_UNITS_DOWNLOAD_REQUESTS,
                          step.step_id)
        step.process_lifecycle()

        self.assertEquals(
            ['http://example.com/deb/{}'.format(x['Filename'])
             for x in pkgs],
            [x.url for x in self.step.step_download_units.downloads])

        # self.assertEquals(
        test_patterns = [
            os.path.join(
                self.pulp_working_dir,
                'worker01/aabb/packages/.*{}'.format(os.path.basename(
                    x['Filename'])))
                for x in pkgs]
        test_values = [x.destination for x in self.step.step_download_units.downloads]
        for pattern, value in zip(test_patterns, test_values):
            self.assertIsNotNone(re.match(pattern, value),
                                 "Mismatching: {} !~ {}".format(pattern, value))

    def test_SaveDownloadedUnits(self):
        self.repo.repo_obj = mock.MagicMock(repo_id=self.repo.id)
        pkgs = self._mock_repometa()
        units = [mock.MagicMock(md5sum=x['MD5sum'],
                                sha1=x['SHA1'],
                                sha256=x['SHA256'],
                                checksum=x['SHA256'],)
                 for x in pkgs]

        dest_dir = os.path.join(self.pulp_working_dir, 'packages')
        os.makedirs(dest_dir)

        path_to_unit = dict()
        for pkg, unit in zip(pkgs, units):
            path = os.path.join(dest_dir, os.path.basename(pkg['Filename']))
            open(path, "wb")
            path_to_unit[path] = unit
            unit.calculate_deb_checksums.return_value = {
                'md5sum': unit.md5sum,
                'sha1': unit.sha1,
                'sha256': unit.sha256,
            }

        self.step.step_download_units.path_to_unit = path_to_unit

        step = self.step.children[7]
        self.assertEquals(constants.SYNC_STEP_SAVE, step.step_id)
        step.process_lifecycle()

        repo = self.repo.repo_obj
        for path, unit in path_to_unit.items():
            unit.save_and_associate.assert_called_once_with(path, repo, force=self.repair_sync)

    def test_SaveDownloadedUnits_bad_sha256(self):
        self.repo.repo_obj = mock.MagicMock(repo_id=self.repo.id)
        # Force a checksum mismatch
        dest_dir = os.path.join(self.pulp_working_dir, 'packages')
        os.makedirs(dest_dir)

        path = os.path.join(dest_dir, "file.deb")
        open(path, "wb")

        unit = mock.MagicMock(sha256='00aa', sha1='bbbb', md5sum='cccc')
        unit.calculate_deb_checksums.return_value = {
            'md5sum': unit.md5sum,
            'sha1': unit.sha1,
            'sha256': "AABB",
        }
        path_to_unit = {path: unit}

        self.step.step_download_units.path_to_unit = path_to_unit

        step = self.step.children[7]
        self.assertEquals(constants.SYNC_STEP_SAVE, step.step_id)
        with self.assertRaises(exceptions.PulpCodedTaskFailedException) as ctx:
            step.process_lifecycle()
        self.assertEquals(
            'Unable to sync repo1 from http://example.com/deb:'
            ' mismatching checksums for file.deb: expected 00aa, actual AABB',
            str(ctx.exception))

    @mock.patch('pulp_deb.plugins.importers.sync.unit_key_to_unit')
    def test_SaveMetadata(self, _UnitKeyToUnit):
        self.step.component_units[self.release]['main'] = mock.MagicMock()
        self.step.component_packages[self.release]['main'] = [
            {'name': 'ape', 'version': '1.2a-4~exp', 'architecture': 'DNA'}]
        self.step.debs_to_check = mock.MagicMock()
        _UnitKeyToUnit.return_value = mock.MagicMock()
        step = self.step.children[8]
        self.assertEquals(constants.SYNC_STEP_SAVE_META, step.step_id)
        step.process_lifecycle()
        self.step.debs_to_check.pop.assert_called_once_with(
            _UnitKeyToUnit.return_value.id, None)
        _UnitKeyToUnit.assert_called_once_with(
            {'name': 'ape', 'version': '1.2a-4~exp', 'architecture': 'DNA'})

    @mock.patch('pulp_deb.plugins.importers.sync.models.DebComponent')
    @mock.patch('pulp_deb.plugins.importers.sync.models.DebRelease')
    @mock.patch('pulp_deb.plugins.importers.sync.gnupg.GPG')
    def test_VerifySignature(self, _GPG, _DebRelease, _DebComponent):
        key_fpr = '0000111122223333444455556666777788889999AAAABBBBCCCCDDDDEEEEFFFF'
        _GPG.return_value.list_keys.return_value = [dict(fingerprint=key_fpr)]
        step = self.step.children[1]
        self.assertEquals(constants.SYNC_STEP_RELEASE_PARSE, step.step_id)
        step.get_config().repo_plugin_config['require_signature'] = True
        step.get_config().repo_plugin_config['allowed_keys'] = key_fpr
        step.process_lifecycle()
        self.assertEqual(_GPG.call_count, 2)
        _GPG.return_value.import_keys.assert_called_once()
        self.assertEqual(_GPG.return_value.export_keys.call_args, mock.call([key_fpr]))
        _GPG.return_value.verify_file.assert_called_once()

    def test_OrphanRemoved(self):
        if self.remove_missing:
            step = self.step.children[9]
            self.assertEquals(constants.SYNC_STEP_ORPHAN_REMOVED_UNITS, step.step_id)
            self.step.conduit.remove_unit = mock.MagicMock()
            step.process_lifecycle()
            self.assertEqual([mock.call(item) for item in self.conduit.get_units.return_value],
                             self.step.conduit.remove_unit.call_args_list)
        else:
            self.assertEqual(9, len(self.step.children))


class TestSyncKeepMissing(_TestSyncBase):
    pass


class TestSyncRemoveMissing(_TestSyncBase):
    remove_missing = True


class TestSyncNestedDistribution(_TestSyncBase):
    release = 'stable/updates'


class TestSyncRepairSync(_TestSyncBase):
    repair_sync = True
