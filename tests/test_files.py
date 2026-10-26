"""Converting files, tasks on a schedule, why the computer is slow: the code that builds commands and
findings (nothing is run here except `systemd-analyze` when it exists)."""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import convert as C, i18n, llm, schedule as S, slow  # noqa: E402
from shellmate.system import ProcGroup  # noqa: E402


class Convert(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = Path(self.dir.name)
        for name in ("clip.mp4", "clip (1).mp3", "a.png", "b.PNG", "photo.heic", "notes.tar.gz", "song.flac"):
            (self.d / name).write_bytes(b"x" * 2048)
        os.truncate(self.d / "clip.mp4", 300 * 1024 * 1024)  # sparse: "bigger than the asked size"
        (self.d / "Downloads").mkdir()
        (self.d / "Downloads" / "talk.mkv").write_bytes(b"x")
        self.enc = mock.patch.object(C, "_ENCODERS", {"libx264", "libvpx-vp9", "libopus", "libmp3lame", "aac"})
        self.enc.start()

    def tearDown(self):
        self.enc.stop()
        self.dir.cleanup()

    def test_plan_from_json(self):
        p = C.Plan.from_json({"operation": "convert", "target": ".JPEG", "max_size_mb": "x", "quality": "ultra"})
        self.assertEqual((p.target, p.max_size_mb, p.quality), ("jpg", 0, ""))
        self.assertEqual(C.Plan.from_json({"operation": "convert", "target": "gif"}).operation, "to_gif")
        self.assertEqual(C.Plan.from_json({"operation": "rm -rf"}).operation, "convert")

    def test_kinds_names_times(self):
        self.assertEqual([C.kind(x) for x in ("a.MP4", "b.tar.zst", "c.heic", "d.opus", "e.txt")],
                         ["video", "archive", "image", "audio", ""])
        self.assertEqual(C.free_path(self.d / "clip.mp3").name, "clip.mp3")
        (self.d / "clip.mp3").write_bytes(b"")
        self.assertEqual(C.free_path(self.d / "clip.mp3").name, "clip (2).mp3")  # "clip (1).mp3" exists too
        self.assertEqual(C.free_path(self.d / "notes.tar.gz").name, "notes (1).tar.gz")
        self.assertEqual([C.seconds(x) for x in ("1:05", "01:02:03", "90", "1,5", "abc", "")], [65, 3723, 90, 1.5, None, None])
        self.assertEqual(C.video_kbps(25, 100, 128), 1864)

    def test_resolve(self):
        found, missing = C.resolve(["*.png", "talk.mkv", "~/x.avi", "загрузки/*.mkv"], self.d, home=self.d)
        self.assertEqual([Path(f).name for f in found], ["a.png", "talk.mkv"])  # glob is case-sensitive, like the shell
        self.assertEqual(missing, ["~/x.avi"])
        self.assertEqual([f.name for f in C.candidates({"image"}, self.d, home=self.d)][:1], ["photo.heic"])

    def test_video_commands(self):
        src = str(self.d / "clip.mp4")
        info = {"duration": 100.0, "video": "h264", "audio": "aac", "width": 3840}
        st = C.video_step(C.Plan("compress", max_size_mb=25), src, info, set())
        cmd = st.argv[0]
        self.assertEqual(cmd[-1], str(self.d / "clip-small.mp4"))
        self.assertIn("-n", cmd)  # never overwrite
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "1928k")
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=1920:-2")  # 4K into a small file: 1080p
        with self.assertRaises(ValueError):
            C.video_step(C.Plan("compress", max_size_mb=0.5), src, {**info, "duration": 3600}, set())
        # mkv takes any codec: no re-encoding
        self.assertIn("copy", C.video_step(C.Plan("convert", target="mkv"), src, info, set()).argv[0])
        st = C.video_step(C.Plan("extract_audio"), src, info, set())
        self.assertEqual((st.argv[0][-3:-1], Path(st.output).name), (["-c:a", "copy"], "clip.m4a"))
        st = C.video_step(C.Plan("trim", start="1:00", end="1:30"), src, info, set())
        self.assertEqual(st.argv[0][st.argv[0].index("-ss") + 1], "60")
        self.assertEqual(st.argv[0][st.argv[0].index("-t") + 1], "30")
        st = C.video_step(C.Plan("to_gif", width=320), src, info, set())
        self.assertTrue(st.output.endswith("clip.gif"))
        self.assertIn("palettegen", " ".join(st.argv[0]))
        with mock.patch.object(C, "_ENCODERS", {"libopenh264", "aac"}):  # Fedora's ffmpeg-free
            self.assertIn("libopenh264", C.video_step(C.Plan("resize", width=1280), src, info, set()).argv[0])

    def test_image_audio_archive(self):
        st, problems = C.steps(C.Plan("convert", target="jpg"), [str(self.d / "a.png"), str(self.d / "photo.heic"),
                                                                 str(self.d / "song.flac")])
        self.assertEqual([Path(s.output).name for s in st], ["a.jpg", "photo.jpg"])
        self.assertEqual(len(problems), 1)  # flac -> jpg makes no sense
        self.assertEqual(C.image_step(C.Plan("compress", target="jpg", max_size_mb=0.5), str(self.d / "a.png"), set()).argv[0][-3:-1],
                         ["-define", "jpeg:extent=512kb"])
        with self.assertRaises(ValueError):
            C.image_step(C.Plan("resize"), str(self.d / "a.png"), set())
        st = C.audio_step(C.Plan("compress"), str(self.d / "song.flac"), {"duration": 200}, set())
        self.assertTrue(st.output.endswith("song-small.mp3"))  # lossless can't be squeezed: mp3
        st = C.archive_step(C.Plan("archive", target="7z"), [str(self.d / "a.png"), str(self.d / "b.PNG")], set())
        self.assertTrue(st.output.endswith(".7z"))
        with mock.patch.object(C, "top_entries", return_value={"notes"}), mock.patch("shutil.which", return_value="/usr/bin/x"):
            st = C.extract_step(str(self.d / "notes.tar.gz"), set())
            self.assertEqual((len(st.argv), st.output), (1, str(self.d / "notes")))  # its own folder: no notes/notes
        with mock.patch.object(C, "top_entries", return_value={"a", "b"}), mock.patch("shutil.which", return_value="/usr/bin/x"):
            st = C.extract_step(str(self.d / "notes.tar.gz"), set())
            self.assertEqual((st.argv[0][0], st.output), ("mkdir", str(self.d / "notes")))


class Schedule(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 10, 30)  # a Monday

    def setUp(self):
        self.lang = i18n.LANG
        i18n.set_lang("ru")

    def tearDown(self):
        i18n.LANG = self.lang

    def when(self, **kw):
        return S.when(S.Spec.from_json({"task": "reminder", **kw}), self.NOW)

    def test_when(self):
        self.assertEqual(self.when(repeat="once", in_minutes=90).after, 5400)
        self.assertEqual(self.when(repeat="once", in_minutes=90).label, "через 1 час 30 минут (в 12:00)")
        self.assertEqual(self.when(repeat="once", time="9:00").calendar, "2026-10-27 09:00:00")  # passed: tomorrow
        self.assertEqual(self.when(repeat="once", time="18:00").calendar, "2026-10-26 18:00:00")
        self.assertEqual(self.when(repeat="once", weekday="fri", time="18:00").calendar, "2026-10-30 18:00:00")
        self.assertEqual(self.when(repeat="once", weekday="mon", time="9:00").calendar, "2026-11-02 09:00:00")
        self.assertEqual(self.when(repeat="weekly", weekday="sun", time="20:00").calendar, "Sun *-*-* 20:00:00")
        self.assertEqual(self.when(repeat="daily").calendar, "*-*-* 10:00:00")
        self.assertTrue(self.when(repeat="login").login)
        for bad in ({"repeat": "once"}, {"repeat": "daily", "time": "25:00"}):
            with self.assertRaises(ValueError):
                self.when(**bad)

    def test_actions_are_safe(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "Downloads").mkdir()
            (home / ".config").mkdir()
            argv, _ = S.action(S.Spec("clean_folder", folder="загрузки", days=7), "jopa", home)
            self.assertEqual(argv[:2], ["find", str(home / "Downloads")])
            self.assertIn("trash", argv)  # to the trash, never rm
            for folder in ("~", "~/.config", "/etc", "~/nope"):
                with self.assertRaises(ValueError):
                    S.action(S.Spec("clean_folder", folder=folder), "jopa", home)
            for cmd in ("sudo pacman -Syu", "rm -rf ~", ""):
                with self.assertRaises(ValueError):
                    S.action(S.Spec("command", command=cmd), "jopa", home)
            self.assertEqual(S.action(S.Spec("command", command="~/bin/sync.sh --all"), "jopa", home)[0],
                             ["/bin/sh", "-c", f"{home}/bin/sync.sh --all"])

    def test_unit_files(self):
        w = S.when(S.Spec("reminder", "daily", "8:00"), self.NOW)
        files = S.unit_files("shellmate-x", "shellmate: 100% готово", ["notify-send", "Напоминание", 'позвонить "маме" $5'], w)
        service, timer = files["shellmate-x.service"], files["shellmate-x.timer"]
        self.assertIn('ExecStart=notify-send Напоминание "позвонить \\"маме\\" $$5"', service)
        self.assertIn("Description=shellmate: 100%% готово", service)
        self.assertIn("OnCalendar=*-*-* 08:00:00\nPersistent=true", timer)
        cmd = S.transient("shellmate-y", "d", ["systemctl", "poweroff"], S.When(after=600))
        self.assertEqual(cmd[:2] + cmd[4:5] + cmd[-3:], ["systemd-run", "--user", "--on-active=600s", "--", "systemctl", "poweroff"])
        self.assertEqual(S.remove_commands("shellmate-y.timer", True), [["systemctl", "--user", "stop", "shellmate-y.timer"]])


class Slow(unittest.TestCase):
    def setUp(self):
        self.lang = i18n.LANG
        i18n.set_lang("en")

    def tearDown(self):
        i18n.LANG = self.lang

    def sample(self, groups=(), **kw) -> slow.Sample:
        mem = {"MemTotal": 16 * 1024**3, "MemAvailable": 8 * 1024**3, "SwapTotal": 0, "SwapFree": 0}
        return slow.Sample(list(groups), 8, {**mem, **kw.pop("mem", {})}, **kw)

    def test_calm_and_busy(self):
        f = slow.diagnose(self.sample([ProcGroup("firefox", [1], 20, 2 * 1024**3)]))
        self.assertEqual([x.level for x in f], ["ok"])
        f = slow.diagnose(self.sample([ProcGroup("node", [1], 99.0, 10**8)]))
        self.assertEqual((f[0].level, f[0].kill), ("warn", "node"))
        f = slow.diagnose(self.sample([ProcGroup("pacman", [1], 40, 10**8), ProcGroup("baloo_file", [2], 60, 10**8)]))
        self.assertEqual(f[-1].level, "info")
        self.assertIn("KDE file indexing", f[-1].text)

    def test_memory_disk_heat_power(self):
        f = slow.diagnose(self.sample([ProcGroup("chrome", [1], 5, 12 * 1024**3)], mem={"MemAvailable": 1024**3}))
        self.assertEqual((f[0].level, f[0].kill), ("bad", "chrome"))
        f = slow.diagnose(self.sample(psi={"io": 45, "io_full": 30}, root_used=97))
        self.assertEqual([x.level for x in f], ["warn", "bad"])
        f = slow.diagnose(self.sample([ProcGroup("ffmpeg", [1], 700, 10**8)], temps=[("CPU", 96.0)], freq=(1400.0, 4800.0)))
        self.assertTrue(any("overheating" in x.text and "1400" in x.text for x in f))
        with mock.patch("shutil.which", return_value="/usr/bin/powerprofilesctl"):
            f = slow.diagnose(self.sample(profile="power-saver"))
        self.assertEqual(f[0].fix, ["powerprofilesctl", "set", "balanced"])

    def test_battery(self):
        bat = {"status": "Discharging", "capacity": "50", "watts": 31.0}
        f = slow.diagnose(self.sample([ProcGroup("steam", [1], 30, 10**8)], battery=bat, dgpu="active",
                                      dgpu_users=["Hyprland", "steam"]), "battery")
        texts = " ".join(x.text for x in f)
        self.assertIn("held by steam", texts)  # the app, not the desktop
        self.assertIn("31 W", texts)
        f = slow.diagnose(self.sample(battery=bat, dgpu="active", dgpu_users=["Hyprland"]), "battery")
        self.assertIn("AQ_DRM_DEVICES", f[0].hint)

    def test_pressure_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "io").write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=8.25 avg60=1 avg300=0 total=1\n")
            self.assertEqual(slow.pressure(Path(d)), {"io": 12.5, "io_full": 8.25})


class Priming(unittest.TestCase):
    def test_prefix_prompt(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "{}"}]
        with mock.patch.object(llm, "_post_raw") as post, mock.patch.dict(llm._FAMILY, {"m": "qwen35", "l": "llama"}):
            llm.prime("http://x", "m", msgs)
            llm.prime("http://x", "l", msgs)  # another template: the prefix wouldn't match, so nothing is sent
        self.assertEqual(post.call_count, 1)
        body = post.call_args[0][2]
        self.assertEqual(body["prompt"], "<|im_start|>system\nS<|im_end|>\n<|im_start|>user\nq<|im_end|>\n"
                                         "<|im_start|>assistant\n{}<|im_end|>\n<|im_start|>user\n.")
        self.assertEqual((body["raw"], body["options"]["num_ctx"]), (True, 8192))  # another num_ctx reloads the model


if __name__ == "__main__":
    unittest.main()
