"""Games: GPU classes, the ProtonDB export, similar-hardware stats, the readiness check."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import games as G  # noqa: E402


def report(appid: str, gpu: str, works: bool, ts: int, **responses) -> dict:
    return {"app": {"steam": {"appId": appid}, "title": "X"}, "timestamp": ts,
            "responses": {"verdict": "yes" if works else "no", **responses}, "systemInfo": {"gpu": gpu}}


class Hardware(unittest.TestCase):
    def test_gpu_class(self):
        cases = {"NVIDIA GeForce RTX 4090": "nvidia rtx40", "GeForce RTX 5050 Max-Q / Mobile": "nvidia rtx50",
                 "NVIDIA GeForce GTX 1660 SUPER": "nvidia gtx16", "NVIDIA GeForce GTX 970": "nvidia gtx9",
                 "AMD Radeon RX 6700 XT (RADV NAVI22)": "amd rdna2", "AMD Radeon RX 7900 XTX": "amd rdna3",
                 "AMD Custom GPU 0405 (RADV VANGOGH)": "steamdeck", "AMD Radeon Graphics (RADV REMBRANDT)": "amd igpu",
                 "AMD Radeon RX 580 Series (RADV POLARIS10)": "amd polaris", "Radeon Pro 555X": "amd polaris",
                 "Intel(R) Arc(tm) A770 Graphics (DG2)": "intel arc", "Mesa Intel(R) UHD Graphics 620 (KBL GT2)": "intel igpu",
                 "llvmpipe (LLVM 15.0.7, 256 bits)": ""}
        for gpu, cls in cases.items():
            self.assertEqual(G.gpu_class(gpu), cls, gpu)
        self.assertEqual(G.class_label("nvidia rtx50"), "NVIDIA RTX 50")
        self.assertEqual(G.class_label("amd rdna3"), "AMD RDNA 3")
        self.assertEqual(G.class_label("amd"), "AMD")

    def test_names_and_proton(self):
        self.assertTrue(G.same_name("The Witcher® 3: Wild Hunt - Game of the Year Edition", "the witcher 3 wild hunt"))
        self.assertFalse(G.same_name("The Witcher 3 REDkit", "The Witcher 3: Wild Hunt"))
        self.assertEqual(G.proton_name({"customProtonVersion": "GE-Proton11-7"}), "GE-Proton11")
        self.assertEqual(G.proton_name({"protonVersion": "9.0-4"}), "Proton 9")
        self.assertEqual(G.proton_name({"protonVersion": "Experimental"}), "Proton Experimental")
        self.assertEqual(G.proton_name({"protonVersion": "Default"}), "")


class Reports(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_stream_and_similar(self):
        now = int(time.time())
        rows = [report("10", "NVIDIA GeForce RTX 5070", True, now - 100, customProtonVersion="GE-Proton10-3",
                       launchOptions="PROTON_ENABLE_NVAPI=1 %command%", concludingNotes="Runs great after enabling NVAPI in the options.")]
        rows += [report("10", "NVIDIA GeForce RTX 5080", True, now - 200 - i, protonVersion="10.0-1",
                        launchOptions="PROTON_ENABLE_NVAPI=1 %command%") for i in range(4)]
        rows += [report("10", "NVIDIA GeForce RTX 3060", False, now - 300), report("10", "AMD Radeon RX 6600", True, now - 400),
                 report("20", "NVIDIA GeForce RTX 5070", True, now - 500), report("10", "NVIDIA GeForce RTX 5070", True, 1000)]
        # the export is one big pretty-printed array; read it in small pieces to cross object boundaries
        data = json.dumps(rows, indent=2).encode()
        stream = io.BufferedReader(io.BytesIO(data), buffer_size=64)
        stream.read = lambda n=-1, r=stream.read: r(min(n, 97) if n and n > 0 else 97)
        parsed = list(G.reports_from(stream, since=now - 10 * 86400))
        self.assertEqual(len(parsed), 8)  # the old report is dropped
        self.assertEqual(parsed[0][:7], (10, now - 100, "nvidia rtx50", "NVIDIA GeForce RTX 5070", 1, 0, "GE-Proton10"))
        db = G.connect()
        db.executemany("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", parsed)
        db.commit()
        db.close()
        sim = G.similar("10", "nvidia rtx50")
        self.assertEqual((sim.label, sim.total, sim.works), ("NVIDIA RTX 50", 5, 5))
        self.assertEqual(sim.proton[0], ("Proton 10", 4))
        self.assertEqual(sim.launch, [("PROTON_ENABLE_NVAPI=1 %command%", 5)])
        self.assertEqual(sim.notes[0][2], "Runs great after enabling NVAPI in the options.")
        # too few of the same generation: the whole vendor
        sim = G.similar("10", "nvidia gtx16")
        self.assertEqual((sim.label, sim.total), ("NVIDIA", 6))
        self.assertIsNone(G.similar("10", "intel arc"))


class Readiness(unittest.TestCase):
    def test_checks(self):
        with tempfile.TemporaryDirectory() as d:
            icd = Path(d) / "icd"
            icd.mkdir()
            (icd / "nvidia_icd.json").write_text("{}")
            conf = Path(d) / "pacman.conf"
            conf.write_text("[core]\nInclude = x\n#[multilib]\n")
            have = {"steam", "gamemoderun"}.__contains__
            checks = G.readiness("arch", ["GeForce RTX 5050 Max-Q / Mobile"], have, set(), icd=icd, pacman_conf=conf, max_map=65530)
            by = {c.what.split(" —")[0]: c for c in checks}
            self.assertTrue(next(c for c in checks if "Vulkan" in c.what).ok)
            multilib = next(c for c in checks if "multilib" in c.what)
            self.assertFalse(multilib.ok)
            self.assertIn("[multilib]", multilib.hint)
            self.assertTrue(next(c for c in checks if c.what == "Steam").ok)
            self.assertEqual(next(c for c in checks if "MangoHud" in c.what).fix, ["mangohud", "lib32-mangohud"])
            self.assertFalse(next(c for c in checks if "max_map_count" in c.what).ok)
            self.assertTrue(by)
            # AMD on Debian without i386: the Vulkan driver package and the architecture hint
            checks = G.readiness("debian", ["AMD Radeon RX 6600"], lambda c: False, {"com.valvesoftware.Steam"},
                                 icd=icd, foreign_arch="")
            self.assertEqual(next(c for c in checks if "Vulkan" in c.what).fix, ["mesa-vulkan-drivers"])
            self.assertIn("dpkg --add-architecture i386", next(c for c in checks if "i386" in c.what).hint)
            self.assertTrue(next(c for c in checks if c.what == "Steam").ok)  # the flatpak counts


if __name__ == "__main__":
    unittest.main()
