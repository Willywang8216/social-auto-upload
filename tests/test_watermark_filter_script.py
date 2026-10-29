"""A long watermark filter graph must not be passed in argv.

Linux caps one argv element at MAX_ARG_STRLEN (128 KiB) and the whole argv at
ARG_MAX. The watermark builder emits one `drawtext=` per timeline window, so a
93-minute video produced a graph over that limit and ffmpeg died with

    OSError: [Errno 7] Argument list too long: 'ffmpeg'

— which is how campaign 3007's prep failed on 2026-10-29 and why the campaign
went to needs_review. ffmpeg's own answer is `-filter_script:v` /
`-filter_complex_script`, which read the identical syntax from a path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from myUtils import media_pipeline


class FilterScriptRewriteTests(unittest.TestCase):
    def test_a_short_graph_is_left_alone(self):
        command = ["ffmpeg", "-y", "-i", "in.mp4", "-vf", "drawtext=text='x'", "out.mp4"]
        argv, script = media_pipeline._run_with_filter_script(list(command))
        self.assertEqual(argv, command)
        self.assertIsNone(script)

    def test_a_command_without_a_filter_is_left_alone(self):
        command = ["ffmpeg", "-i", "in.mp4", "-c", "copy", "out.mp4"]
        self.assertEqual(media_pipeline._run_with_filter_script(list(command)), (command, None))

    def test_a_long_vf_goes_to_a_script_file(self):
        graph = "drawtext=text='wm'," * 5000
        command = ["ffmpeg", "-y", "-i", "in.mp4", "-vf", graph, "-c:a", "copy", "out.mp4"]
        argv, script = media_pipeline._run_with_filter_script(command)
        self.assertIsNotNone(script)
        self.addCleanup(os.unlink, script)
        self.assertIn("-filter_script:v", argv)
        self.assertNotIn(graph, argv)
        self.assertEqual(Path(script).read_text(encoding="utf-8"), graph)
        # the argv is now tiny regardless of how long the graph is
        self.assertLess(sum(len(part) for part in argv), 1000)

    def test_a_long_filter_complex_uses_its_own_script_flag(self):
        graph = "overlay=1:1," * 5000
        command = ["ffmpeg", "-y", "-i", "a", "-filter_complex", graph, "-map", "[v]", "o.mp4"]
        argv, script = media_pipeline._run_with_filter_script(command)
        self.addCleanup(os.unlink, script)
        self.assertIn("-filter_complex_script", argv)
        self.assertNotIn("-filter_complex", argv)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class FilterScriptRunsTests(unittest.TestCase):
    def test_ffmpeg_accepts_the_rewritten_command(self):
        """The real proof: run it, and confirm the watermark is applied."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "in.mp4"
        build = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc=size=128x128:rate=10", "-t", "1", "-pix_fmt", "yuv420p", str(source)],
            capture_output=True,
        )
        if build.returncode != 0:
            self.skipTest("could not build a source clip")
        # A graph well past the threshold, built the way the watermark builder
        # does: many drawtext windows.
        graph = ",".join(
            f"drawtext=text='wm':x=10:y=10:fontsize=12:fontcolor=white@0.5:"
            f"enable='between(t,{i / 100:.2f},{(i + 1) / 100:.2f})'"
            for i in range(4000)
        )
        command = ["ffmpeg", "-y", "-i", str(source), "-vf", graph, "-c:a", "copy",
                   str(root / "out.mp4")]
        argv, script = media_pipeline._run_with_filter_script(command)
        self.assertIsNotNone(script)
        self.addCleanup(os.unlink, script)
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertTrue((root / "out.mp4").is_file())
        self.assertGreater((root / "out.mp4").stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
