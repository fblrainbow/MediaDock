"""Stage-004 file policy tests (T410/T411/T422-adjacent). No network."""
import os
import tempfile
import time
import unittest
from typing import cast

from core_files import (cleanup_task_files, discover_task_files, is_inside,
                        is_temp_file, started_epoch, task_file_size,
                        video_id_from_url)

URL = "https://www.youtube.com/watch?v=abcdefghijk"
VID = "abcdefghijk"


def make_file(directory, name, content="x", age_seconds=0.0):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


class TestVideoId(unittest.TestCase):
    def test_watch(self):
        self.assertEqual(video_id_from_url(URL), VID)
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/watch?list=1&v=abcdefghijk"),
            VID)


class TestTaskFileSize(unittest.TestCase):
    """Stage-013: the delivered file size shown by the task list."""

    def test_size_of_delivered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].mp4" % VID, "x" * 1234)
            self.assertEqual(task_file_size(tmp, URL), 1234)

    def test_temp_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].mp4.part" % VID, "x" * 999)
            self.assertEqual(task_file_size(tmp, URL), 0)

    def test_largest_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].webm" % VID, "x" * 500)
            make_file(tmp, "clip [%s].mp3" % VID, "x" * 900)
            self.assertEqual(task_file_size(tmp, URL), 900)

    def test_prefer_picks_the_matching_deliverable(self):
        """Stage-013: a download task must not borrow the extracted mp3 size."""
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].webm" % VID, "x" * 500)
            make_file(tmp, "clip [%s].mp3" % VID, "x" * 900)
            self.assertEqual(task_file_size(tmp, URL, "video"), 500)
            self.assertEqual(task_file_size(tmp, URL, "audio"), 900)

    def test_prefer_falls_back_when_that_kind_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].webm" % VID, "x" * 500)
            self.assertEqual(task_file_size(tmp, URL, "audio"), 500)
            self.assertEqual(task_file_size(tmp, URL, "video"), 500)

    def test_prefer_ignores_unknown_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "clip [%s].webm" % VID, "x" * 500)
            self.assertEqual(task_file_size(tmp, URL, "nonsense"), 500)

    def test_other_videos_and_unknown_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "other [zzzzzzzzzzz].mp4", "x" * 77)
            self.assertEqual(task_file_size(tmp, URL), 0)
            self.assertEqual(task_file_size(tmp, "https://example.com/x"), 0)

    def test_missing_directory_is_safe(self):
        self.assertEqual(task_file_size("Z:\\nope-not-here", URL), 0)

    def test_shorts_and_youtu_be(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/shorts/abcdefghijk"), VID)
        self.assertEqual(video_id_from_url("https://youtu.be/abcdefghijk"), VID)

    def test_unknown_urls(self):
        for url in ("", "https://example.com/v", "not a url",
                    "https://www.youtube.com/watch?v=short"):
            self.assertEqual(video_id_from_url(url), "")

    def test_none_is_tolerated(self):
        # 运行期容忍 None（调用方可能传空），类型上显式声明
        self.assertEqual(video_id_from_url(cast(str, None)), "")


class TestPathBoundary(unittest.TestCase):
    def test_inside(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        child = os.path.join(root, "a", "b.mp4")
        self.assertTrue(is_inside(root, os.path.join(root, "b.mp4")))
        self.assertTrue(is_inside(root, child))

    def test_outside(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        other = tempfile.mkdtemp(prefix="mediadock-other-")
        self.assertFalse(is_inside(root, os.path.join(other, "b.mp4")))
        self.assertFalse(is_inside(root, os.path.join(root, "..", "escape.mp4")))
        self.assertFalse(is_inside(root, ""))
        self.assertFalse(is_inside("", "/etc/passwd"))

    def test_temp_suffixes(self):
        self.assertTrue(is_temp_file("x.mp4.part"))
        self.assertTrue(is_temp_file("x.ytdl"))
        self.assertFalse(is_temp_file("x.mp4"))


class TestStartedEpoch(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertGreater(started_epoch("2026-09-19T10:00:00"), 0)
        self.assertEqual(started_epoch(""), 0.0)
        self.assertEqual(started_epoch(None), 0.0)
        self.assertEqual(started_epoch("nonsense"), 0.0)


class TestDiscoverAndCleanup(unittest.TestCase):
    def test_finds_same_video_files_only(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        mine = make_file(root, f"video [{VID}].mp4")
        mine_part = make_file(root, f"video [{VID}].mp4.part")
        make_file(root, "other [zzzzzzzzzzz].mp4")
        found = set(discover_task_files(root, URL))
        self.assertEqual(found, {os.path.realpath(mine),
                                 os.path.realpath(mine_part)})

    def test_old_files_are_not_selected(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        old = make_file(root, f"video [{VID}].mp4", age_seconds=3600)
        since = time.time() - 60
        self.assertEqual(discover_task_files(root, URL, (), since), [])
        self.assertTrue(os.path.exists(old))

    def test_explicit_artifacts_bypass_id_scan(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        odd = make_file(root, "clip without id.mp4")
        found = discover_task_files(root, "", [odd])
        self.assertEqual(found, [os.path.realpath(odd)])

    def test_cleanup_deletes_inside_and_reports(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        keep_old = make_file(root, f"video [{VID}].mp4", age_seconds=7200)
        gone = make_file(root, f"video [{VID}].mp4.part")
        since = time.time() - 60
        deleted = cleanup_task_files(root, URL, [gone], since)
        self.assertEqual(set(deleted), {os.path.realpath(gone)})
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.exists(keep_old))

    def test_cleanup_without_window_is_refused(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        path = make_file(root, f"video [{VID}].mp4.part")
        self.assertEqual(cleanup_task_files(root, URL, [path], 0.0), [])
        self.assertTrue(os.path.exists(path))

    def test_cleanup_refuses_outside_path(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        other = tempfile.mkdtemp(prefix="mediadock-other-")
        outside = make_file(other, f"video [{VID}].mp4")
        deleted = cleanup_task_files(root, URL, [outside], time.time() - 60)
        self.assertEqual(deleted, [])
        self.assertTrue(os.path.exists(outside), "outside file was deleted")

    def test_cleanup_is_idempotent(self):
        root = tempfile.mkdtemp(prefix="mediadock-files-")
        path = make_file(root, f"video [{VID}].mp4.part")
        since = time.time() - 60
        self.assertEqual(len(cleanup_task_files(root, URL, [path], since)), 1)
        self.assertEqual(cleanup_task_files(root, URL, [path], since), [])


if __name__ == "__main__":
    unittest.main()
