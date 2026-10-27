import tempfile
import unittest
from pathlib import Path
from unittest import mock

import omni


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img1, self.img2, self.ref, self.clip = (self.tmp / n for n in ("a.png", "b.png", "ref.png", "clip.mp4"))
        for path in (self.img1, self.img2, self.ref, self.clip):
            path.write_bytes(b"fake-media-bytes")

    def test_video_edit_with_image_ref_composes_sources_then_references(self):
        pkt = omni.build_packet("Make <VIDEO_0> pixel art like <IMAGE_REF_0>.", video=self.clip,
                                image_refs=[self.ref], strict_duration=False)
        self.assertEqual(pkt["task"], "edit")
        self.assertTrue(pkt["composed_prompt"].startswith(
            "[# Sources <VIDEO_0>@Video1] [# References <IMAGE_REF_0>@Image1] Make <VIDEO_0>"))
        self.assertEqual([a["alias"] for a in pkt["attachments"]], ["Video1", "Image1"])
        self.assertEqual(pkt["target_url"], omni.AISTUDIO_URL)

    def test_first_and_last_frame(self):
        pkt = omni.build_packet("Orbit.", first_frame=self.img1, last_frame=self.img2)
        self.assertEqual(pkt["task"], "image_to_video")
        self.assertIn("[# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image2]", pkt["composed_prompt"])
        self.assertIn("Use Image1 as the starting frame and Image2 as the ending frame.", pkt["composed_prompt"])

    def test_loop_frame_binds_one_image_twice(self):
        pkt = omni.build_packet("Loop.", loop_frame=self.img1)
        self.assertIn("[# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image1]", pkt["composed_prompt"])
        self.assertEqual(len(pkt["attachments"]), 1)

    def test_extend_uses_previous_video(self):
        pkt = omni.build_packet("Continue.", extend_video=self.clip, strict_duration=False)
        self.assertEqual(pkt["task"], "extend")
        self.assertIn("[# Sources <PREVIOUS_VIDEO>@Video1]", pkt["composed_prompt"])

    def test_invalid_combinations_are_rejected(self):
        with self.assertRaises(ValueError):
            omni.build_packet("x", last_frame=self.img2)
        with self.assertRaises(ValueError):
            omni.build_packet("x", video=self.clip, extend_video=self.clip)
        with self.assertRaises(ValueError):
            omni.build_packet("x", image_refs=[self.ref] * 7)
        with self.assertRaises(FileNotFoundError):
            omni.build_packet("x", video=self.tmp / "missing.mp4")

    def test_overlong_source_video_is_rejected_unless_lenient(self):
        with mock.patch.object(omni, "probe_video_duration", return_value=12.0):
            with self.assertRaises(ValueError):
                omni.build_packet("x", video=self.clip)
            self.assertEqual(len(omni.build_packet("x", video=self.clip, strict_duration=False)["warnings"]), 1)

    def test_default_duration_follows_the_source_clamped_to_3_10(self):
        self.assertEqual(omni.default_duration({"attachments": [{"kind": "video", "duration_sec": 3.2}]}), 4)
        self.assertEqual(omni.default_duration({"attachments": [{"kind": "video", "duration_sec": 1.0}]}), 3)
        self.assertEqual(omni.default_duration({"attachments": [{"kind": "video", "duration_sec": 14.0}]}), 10)
        self.assertIsNone(omni.default_duration({"attachments": [{"kind": "image"}]}))


class RunTests(unittest.TestCase):
    def test_existing_output_is_never_overwritten(self):
        out = Path(tempfile.mkdtemp())
        (out / "output.mp4").write_bytes(b"done")
        with self.assertRaises(FileExistsError):
            omni.run({"composed_prompt": "x", "target_url": omni.AISTUDIO_URL, "prompt_sha256": "0"}, out)
        self.assertEqual((out / "output.mp4").read_bytes(), b"done")
        self.assertFalse((out / "state.json").exists())

    def test_account_dialogs_stop_the_run_instead_of_being_clicked(self):
        class Page:
            def __init__(self, visible):
                self.visible = visible

            def get_by_text(self, text):
                return mock.Mock(count=mock.Mock(return_value=int(text == self.visible)))

        self.assertEqual(omni.blocking_dialog(Page("Temporary Chat is not supported"))["status"], "drive_required")
        self.assertEqual(omni.blocking_dialog(Page("Start creating with media"))["status"], "consent_required")
        self.assertIsNone(omni.blocking_dialog(Page(None)))


if __name__ == "__main__":
    unittest.main()
