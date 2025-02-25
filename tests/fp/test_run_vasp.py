import json
import shutil
import unittest
from pathlib import (
    Path,
)

import numpy as np
from dflow.python import (
    OP,
    OPIO,
    Artifact,
    OPIOSign,
    TransientError,
)
from mock import (
    call,
    mock,
    patch,
)

from dpgen2.constants import (
    fp_default_log_name,
    fp_default_out_data_name,
)
from dpgen2.fp import (
    RunVasp,
)
from dpgen2.fp.vasp import (
    vasp_conf_name,
    vasp_input_name,
    vasp_kp_name,
    vasp_pot_name,
)

from .context import (
    dpgen2,
)


class TestRunVasp(unittest.TestCase):
    def setUp(self):
        self.task_path = Path("task/path")
        self.task_path.mkdir(parents=True, exist_ok=True)
        (self.task_path / vasp_conf_name).write_text("foo")
        (self.task_path / vasp_input_name).write_text("bar")
        (self.task_path / vasp_pot_name).write_text("dee")
        (self.task_path / vasp_kp_name).write_text("por")
        self.task_name = "task_000"

    def tearDown(self):
        if Path("task").is_dir():
            shutil.rmtree("task")
        if Path(self.task_name).is_dir():
            shutil.rmtree(self.task_name)

    @patch("dpgen2.fp.vasp.run_command")
    def test_success(self, mocked_run):
        mocked_run.side_effect = [(0, "foo\n", "")]
        op = RunVasp()

        def new_to(obj, foo, bar):
            data_path = Path("data")
            data_path.mkdir()
            (data_path / "foo").write_text("bar")

        def new_init(obj, foo):
            pass

        with mock.patch.object(dpgen2.fp.vasp.dpdata.LabeledSystem, "to", new=new_to):
            with mock.patch.object(
                dpgen2.fp.vasp.dpdata.LabeledSystem, "__init__", new=new_init
            ):
                out = op.execute(
                    OPIO(
                        {
                            "config": {
                                "run": {
                                    "command": "myvasp",
                                    "log": "foo.log",
                                    "out": "data",
                                }
                            },
                            "task_name": self.task_name,
                            "task_path": self.task_path,
                        }
                    )
                )
        work_dir = Path(self.task_name)
        # check output
        self.assertEqual(out["log"], work_dir / "foo.log")
        self.assertEqual(out["labeled_data"], work_dir / "data")
        # check call
        calls = [
            call(" ".join(["myvasp", ">", "foo.log"]), shell=True),
        ]
        mocked_run.assert_has_calls(calls)
        # check input files are correctly linked
        self.assertEqual((work_dir / vasp_conf_name).read_text(), "foo")
        self.assertEqual((work_dir / vasp_input_name).read_text(), "bar")
        self.assertEqual((work_dir / vasp_pot_name).read_text(), "dee")
        self.assertEqual((work_dir / vasp_kp_name).read_text(), "por")
        # check output
        self.assertEqual((Path(self.task_name) / "data" / "foo").read_text(), "bar")

    @patch("dpgen2.fp.vasp.run_command")
    def test_success_1(self, mocked_run):
        mocked_run.side_effect = [(0, "foo\n", "")]
        op = RunVasp()

        def new_to(obj, foo, bar):
            data_path = Path("data")
            data_path.mkdir()
            (data_path / "foo").write_text("bar")

        def new_init(obj, foo):
            pass

        with mock.patch.object(dpgen2.fp.vasp.dpdata.LabeledSystem, "to", new=new_to):
            with mock.patch.object(
                dpgen2.fp.vasp.dpdata.LabeledSystem, "__init__", new=new_init
            ):
                out = op.execute(
                    OPIO(
                        {
                            "config": {
                                "run": {
                                    "command": "myvasp",
                                }
                            },
                            "task_name": self.task_name,
                            "task_path": self.task_path,
                        }
                    )
                )
        work_dir = Path(self.task_name)
        # check output
        self.assertEqual(out["log"], work_dir / fp_default_log_name)
        self.assertEqual(out["labeled_data"], work_dir / fp_default_out_data_name)
        # check call
        calls = [
            call(" ".join(["myvasp", ">", fp_default_log_name]), shell=True),
        ]
        mocked_run.assert_has_calls(calls)
        # check input files are correctly linked
        self.assertEqual((work_dir / vasp_conf_name).read_text(), "foo")
        self.assertEqual((work_dir / vasp_input_name).read_text(), "bar")
        self.assertEqual((work_dir / vasp_pot_name).read_text(), "dee")
        self.assertEqual((work_dir / vasp_kp_name).read_text(), "por")
        # check output
        self.assertEqual((Path(self.task_name) / "data" / "foo").read_text(), "bar")

    @patch("dpgen2.fp.vasp.run_command")
    def test_error(self, mocked_run):
        mocked_run.side_effect = [(1, "foo\n", "")]
        op = RunVasp()
        with self.assertRaises(TransientError) as ee:
            out = op.execute(
                OPIO(
                    {
                        "config": {
                            "run": {
                                "command": "myvasp",
                            }
                        },
                        "task_name": self.task_name,
                        "task_path": self.task_path,
                    }
                )
            )
        # check call
        calls = [
            call(" ".join(["myvasp", ">", fp_default_log_name]), shell=True),
        ]
        mocked_run.assert_has_calls(calls)
