import copy
import glob
import json
import logging
import os
import pickle
from pathlib import (
    Path,
)
from typing import (
    Dict,
    List,
    Optional,
    Union,
)

import dpdata
from dflow import (
    InputArtifact,
    InputParameter,
    Inputs,
    OutputArtifact,
    OutputParameter,
    Outputs,
    S3Artifact,
    Step,
    Steps,
    Workflow,
    argo_range,
    download_artifact,
    upload_artifact,
)
from dflow.python import (
    OP,
    OPIO,
    Artifact,
    FatalError,
    OPIOSign,
    PythonOPTemplate,
    TransientError,
    upload_packages,
)

from dpgen2.conf import (
    conf_styles,
)
from dpgen2.constants import (
    default_host,
    default_image,
)
from dpgen2.entrypoint.args import normalize as normalize_args
from dpgen2.entrypoint.common import (
    expand_idx,
    expand_sys_str,
    global_config_workflow,
)
from dpgen2.exploration.render import (
    TrajRenderLammps,
)
from dpgen2.exploration.report import (
    ExplorationReportTrustLevelsRandom,
    conv_styles,
)
from dpgen2.exploration.scheduler import (
    ConvergenceCheckStageScheduler,
    ExplorationScheduler,
)
from dpgen2.exploration.selector import (
    ConfSelectorFrames,
)
from dpgen2.exploration.task import (
    ExplorationStage,
    ExplorationTask,
    LmpTemplateTaskGroup,
    NPTTaskGroup,
    make_task_group_from_config,
)
from dpgen2.flow import (
    ConcurrentLearning,
)
from dpgen2.fp import (
    fp_styles,
)
from dpgen2.op import (
    CollectData,
    PrepDPTrain,
    PrepLmp,
    RunDPTrain,
    RunLmp,
    SelectConfs,
)
from dpgen2.superop import (
    ConcurrentLearningBlock,
    PrepRunDPTrain,
    PrepRunFp,
    PrepRunLmp,
)
from dpgen2.utils import (
    BinaryFileInput,
    bohrium_config_from_dict,
    dump_object_to_file,
    get_subkey,
    load_object_from_file,
    matched_step_key,
    print_keys_in_nice_format,
    sort_slice_ops,
    workflow_config_from_dict,
)
from dpgen2.utils.step_config import normalize as normalize_step_dict

default_config = normalize_step_dict(
    {
        "template_config": {
            "image": default_image,
        }
    }
)


def make_concurrent_learning_op(
    train_style: str = "dp",
    explore_style: str = "lmp",
    fp_style: str = "vasp",
    prep_train_config: dict = default_config,
    run_train_config: dict = default_config,
    prep_explore_config: dict = default_config,
    run_explore_config: dict = default_config,
    prep_fp_config: dict = default_config,
    run_fp_config: dict = default_config,
    select_confs_config: dict = default_config,
    collect_data_config: dict = default_config,
    cl_step_config: dict = default_config,
    upload_python_packages: Optional[List[os.PathLike]] = None,
):
    if train_style in ("dp", "dp-dist"):
        prep_run_train_op = PrepRunDPTrain(
            "prep-run-dp-train",
            PrepDPTrain,
            RunDPTrain,
            prep_config=prep_train_config,
            run_config=run_train_config,
            upload_python_packages=upload_python_packages,
        )
    else:
        raise RuntimeError(f"unknown train_style {train_style}")
    if explore_style == "lmp":
        prep_run_explore_op = PrepRunLmp(
            "prep-run-lmp",
            PrepLmp,
            RunLmp,
            prep_config=prep_explore_config,
            run_config=run_explore_config,
            upload_python_packages=upload_python_packages,
        )
    else:
        raise RuntimeError(f"unknown explore_style {explore_style}")

    if fp_style in fp_styles.keys():
        prep_run_fp_op = PrepRunFp(
            f"prep-run-fp",
            fp_styles[fp_style]["prep"],
            fp_styles[fp_style]["run"],
            prep_config=prep_fp_config,
            run_config=run_fp_config,
            upload_python_packages=upload_python_packages,
        )
    else:
        raise RuntimeError(f"unknown fp_style {fp_style}")

    # ConcurrentLearningBlock
    block_cl_op = ConcurrentLearningBlock(
        "concurrent-learning-block",
        prep_run_train_op,
        prep_run_explore_op,
        SelectConfs,
        prep_run_fp_op,
        CollectData,
        select_confs_config=select_confs_config,
        collect_data_config=collect_data_config,
        upload_python_packages=upload_python_packages,
    )
    # dpgen
    dpgen_op = ConcurrentLearning(
        "concurrent-learning",
        block_cl_op,
        upload_python_packages=upload_python_packages,
        step_config=cl_step_config,
    )

    return dpgen_op


def make_naive_exploration_scheduler(
    config,
    old_style=False,
):
    # use npt task group
    model_devi_jobs = (
        config["model_devi_jobs"] if old_style else config["explore"]["stages"]
    )
    sys_configs = (
        config["sys_configs"] if old_style else config["explore"]["configurations"]
    )
    sys_prefix = config.get("sys_prefix")
    if sys_prefix is not None:
        for ii in range(len(sys_configs)):
            if isinstance(sys_configs[ii], list):
                sys_configs[ii] = [
                    os.path.join(sys_prefix, jj) for jj in sys_prefix[ii]
                ]
    mass_map = config["mass_map"] if old_style else config["inputs"]["mass_map"]
    type_map = config["type_map"] if old_style else config["inputs"]["type_map"]
    numb_models = config["numb_models"] if old_style else config["train"]["numb_models"]
    fp_task_max = config["fp_task_max"] if old_style else config["fp"]["task_max"]
    max_numb_iter = (
        config["max_numb_iter"] if old_style else config["explore"]["max_numb_iter"]
    )
    fatal_at_max = (
        config.get("fatal_at_max", True)
        if old_style
        else config["explore"]["fatal_at_max"]
    )
    convergence = config["explore"]["convergence"]
    output_nopbc = False if old_style else config["explore"]["output_nopbc"]
    scheduler = ExplorationScheduler()
    # report
    conv_style = convergence.pop("type")
    report = conv_styles[conv_style](**convergence)
    render = TrajRenderLammps(nopbc=output_nopbc)
    # selector
    selector = ConfSelectorFrames(
        render,
        report,
        fp_task_max,
    )

    sys_configs_lmp = []
    for sys_config in sys_configs:
        conf_style = sys_config.pop("type")
        generator = conf_styles[conf_style](**sys_config)
        sys_configs_lmp.append(generator.get_file_content(type_map))

    for job_ in model_devi_jobs:
        if not isinstance(job_, list):
            job = [job_]
        else:
            job = job_
        # stage
        stage = ExplorationStage()
        for jj in job:
            n_sample = jj.pop("n_sample")
            ##  ignore the expansion of sys_idx
            # get all file names of md initial configurations
            try:
                sys_idx = jj.pop("sys_idx")
            except KeyError:
                sys_idx = jj.pop("conf_idx")
            conf_list = []
            for ii in sys_idx:
                conf_list += sys_configs_lmp[ii]
            # make task group
            tgroup = make_task_group_from_config(numb_models, mass_map, jj)
            # add the list to task group
            tgroup.set_conf(
                conf_list,
                n_sample=n_sample,
                random_sample=True,
            )
            tasks = tgroup.make_task()
            stage.add_task_group(tasks)
        # stage_scheduler
        stage_scheduler = ConvergenceCheckStageScheduler(
            stage,
            selector,
            max_numb_iter=max_numb_iter,
            fatal_at_max=fatal_at_max,
        )
        # scheduler
        scheduler.add_stage_scheduler(stage_scheduler)

    return scheduler


def get_kspacing_kgamma_from_incar(
    fname,
):
    with open(fname) as fp:
        lines = fp.readlines()
    ks = None
    kg = None
    for ii in lines:
        if "KSPACING" in ii:
            ks = float(ii.split("=")[1])
        if "KGAMMA" in ii:
            if "T" in ii.split("=")[1]:
                kg = True
            elif "F" in ii.split("=")[1]:
                kg = False
            else:
                raise RuntimeError(f"invalid kgamma value {ii.split('=')[1]}")
    assert ks is not None and kg is not None
    return ks, kg


def make_optional_parameter(
    mixed_type=False,
):
    return {"data_mixed_type": mixed_type}


def workflow_concurrent_learning(
    config: Dict,
    old_style: bool = False,
):
    default_config = (
        normalize_step_dict(config.get("default_config", {}))
        if old_style
        else config["default_step_config"]
    )

    train_style = (
        config.get("train_style", "dp") if old_style else config["train"]["type"]
    )
    explore_style = (
        config.get("explore_style", "lmp") if old_style else config["explore"]["type"]
    )
    fp_style = config.get("fp_style", "vasp") if old_style else config["fp"]["type"]
    prep_train_config = (
        normalize_step_dict(config.get("prep_train_config", default_config))
        if old_style
        else config["step_configs"]["prep_train_config"]
    )
    run_train_config = (
        normalize_step_dict(config.get("run_train_config", default_config))
        if old_style
        else config["step_configs"]["run_train_config"]
    )
    prep_explore_config = (
        normalize_step_dict(config.get("prep_explore_config", default_config))
        if old_style
        else config["step_configs"]["prep_explore_config"]
    )
    run_explore_config = (
        normalize_step_dict(config.get("run_explore_config", default_config))
        if old_style
        else config["step_configs"]["run_explore_config"]
    )
    prep_fp_config = (
        normalize_step_dict(config.get("prep_fp_config", default_config))
        if old_style
        else config["step_configs"]["prep_fp_config"]
    )
    run_fp_config = (
        normalize_step_dict(config.get("run_fp_config", default_config))
        if old_style
        else config["step_configs"]["run_fp_config"]
    )
    select_confs_config = (
        normalize_step_dict(config.get("select_confs_config", default_config))
        if old_style
        else config["step_configs"]["select_confs_config"]
    )
    collect_data_config = (
        normalize_step_dict(config.get("collect_data_config", default_config))
        if old_style
        else config["step_configs"]["collect_data_config"]
    )
    cl_step_config = (
        normalize_step_dict(config.get("cl_step_config", default_config))
        if old_style
        else config["step_configs"]["cl_step_config"]
    )
    upload_python_packages = config.get("upload_python_packages", None)

    if train_style == "dp":
        init_models_paths = (
            config.get("training_iter0_model_path", None)
            if old_style
            else config["train"].get("init_models_paths", None)
        )
        numb_models = (
            config["numb_models"] if old_style else config["train"]["numb_models"]
        )
        if init_models_paths is not None and len(init_models_paths) != numb_models:
            raise RuntimeError(
                f"{len(init_models_paths)} init models provided, which does "
                "not match numb_models={numb_models}"
            )
    elif train_style == "dp-dist" and not old_style:
        init_models_paths = [config["train"].get("student_model_path", None)]
        config["train"]["numb_models"] = 1
    else:
        raise RuntimeError(
            f"unknown params, train_style: {train_style}, old_style: {old_style}"
        )

    if upload_python_packages is not None and isinstance(upload_python_packages, str):
        upload_python_packages = [upload_python_packages]
    if upload_python_packages is not None:
        _upload_python_packages: List[os.PathLike] = [
            Path(ii) for ii in upload_python_packages
        ]
        upload_python_packages = _upload_python_packages

    concurrent_learning_op = make_concurrent_learning_op(
        train_style,
        explore_style,
        fp_style,
        prep_train_config=prep_train_config,
        run_train_config=run_train_config,
        prep_explore_config=prep_explore_config,
        run_explore_config=run_explore_config,
        prep_fp_config=prep_fp_config,
        run_fp_config=run_fp_config,
        select_confs_config=select_confs_config,
        collect_data_config=collect_data_config,
        cl_step_config=cl_step_config,
        upload_python_packages=upload_python_packages,
    )
    scheduler = make_naive_exploration_scheduler(config, old_style=old_style)

    type_map = config["type_map"] if old_style else config["inputs"]["type_map"]
    numb_models = config["numb_models"] if old_style else config["train"]["numb_models"]
    template_script_ = (
        config["default_training_param"]
        if old_style
        else config["train"]["template_script"]
    )
    if isinstance(template_script_, list):
        template_script = [json.loads(Path(ii).read_text()) for ii in template_script_]
    else:
        template_script = json.loads(Path(template_script_).read_text())
    train_config = {} if old_style else config["train"]["config"]
    lmp_config = (
        config.get("lmp_config", {}) if old_style else config["explore"]["config"]
    )
    if (
        "teacher_model_path" in lmp_config
        and lmp_config["teacher_model_path"] is not None
    ):
        assert os.path.exists(
            lmp_config["teacher_model_path"]
        ), f"No such file: {lmp_config['teacher_model_path']}"
        lmp_config["teacher_model_path"] = BinaryFileInput(
            lmp_config["teacher_model_path"], "pb"
        )

    fp_config = config.get("fp_config", {}) if old_style else {}
    if old_style:
        potcar_names = config["fp_pp_files"]
        incar_template_name = config["fp_incar"]
        kspacing, kgamma = get_kspacing_kgamma_from_incar(incar_template_name)
        fp_inputs_config = {
            "kspacing": kspacing,
            "kgamma": kgamma,
            "incar_template_name": incar_template_name,
            "potcar_names": potcar_names,
        }
    else:
        fp_inputs_config = config["fp"]["inputs_config"]
    fp_inputs = fp_styles[fp_style]["inputs"](**fp_inputs_config)

    fp_config["inputs"] = fp_inputs
    fp_config["run"] = config["fp"]["run_config"]
    if fp_style == "deepmd":
        assert (
            "teacher_model_path" in fp_config["run"]
        ), f"Cannot find 'teacher_model_path' in config['fp']['run_config'] when fp_style == 'deepmd'"
        assert os.path.exists(
            fp_config["run"]["teacher_model_path"]
        ), f"No such file: {fp_config['run']['teacher_model_path']}"
        fp_config["run"]["teacher_model_path"] = BinaryFileInput(
            fp_config["run"]["teacher_model_path"], "pb"
        )

    init_data_prefix = (
        config.get("init_data_prefix")
        if old_style
        else config["inputs"]["init_data_prefix"]
    )
    init_data = (
        config["init_data_sys"] if old_style else config["inputs"]["init_data_sys"]
    )
    if init_data_prefix is not None:
        init_data = [os.path.join(init_data_prefix, ii) for ii in init_data]
    if isinstance(init_data, str):
        init_data = expand_sys_str(init_data)
    init_data = upload_artifact(init_data)
    iter_data = upload_artifact([])
    if init_models_paths is not None:
        init_models = upload_artifact(init_models_paths)
    else:
        init_models = None

    optional_parameter = make_optional_parameter(
        config["inputs"]["mixed_type"],
    )

    # here the scheduler is passed as input parameter to the concurrent_learning_op
    dpgen_step = Step(
        "dpgen-step",
        template=concurrent_learning_op,
        parameters={
            "type_map": type_map,
            "numb_models": numb_models,
            "template_script": template_script,
            "train_config": train_config,
            "lmp_config": lmp_config,
            "fp_config": fp_config,
            "exploration_scheduler": scheduler,
            "optional_parameter": optional_parameter,
        },
        artifacts={
            "init_models": init_models,
            "init_data": init_data,
            "iter_data": iter_data,
        },
    )
    return dpgen_step


def get_scheduler_ids(
    reuse_step,
):
    scheduler_ids = []
    for idx, ii in enumerate(reuse_step):
        if get_subkey(ii.key, 1) == "scheduler":
            scheduler_ids.append(idx)
    scheduler_keys = [reuse_step[ii].key for ii in scheduler_ids]
    assert (
        sorted(scheduler_keys) == scheduler_keys
    ), "The scheduler keys are not properly sorted"

    if len(scheduler_ids) == 0:
        logging.warning(
            "No scheduler found in the workflow, " "does not do any replacement."
        )
    return scheduler_ids


def update_reuse_step_scheduler(
    reuse_step,
    scheduler_new,
):
    scheduler_ids = get_scheduler_ids(reuse_step)
    if len(scheduler_ids) == 0:
        return reuse_step

    # do replacement
    reuse_step[scheduler_ids[-1]].modify_output_parameter(
        "exploration_scheduler", scheduler_new
    )

    return reuse_step


def copy_scheduler_plans(
    scheduler_new,
    scheduler_old,
):
    if len(scheduler_old.stage_schedulers) == 0:
        return scheduler_new
    if len(scheduler_new.stage_schedulers) < len(scheduler_old.stage_schedulers):
        raise RuntimeError(
            "The new scheduler has less stages than the old scheduler, "
            "scheduler copy is not supported."
        )
    # the scheduler_old is planned. minic the init call of the scheduler
    if scheduler_old.get_iteration() > -1:
        scheduler_new.plan_next_iteration()
    for ii in range(len(scheduler_old.stage_schedulers)):
        old_stage = scheduler_old.stage_schedulers[ii]
        old_reports = old_stage.get_reports()
        if old_stage.next_iteration() > 0:
            if ii != scheduler_new.get_stage():
                raise RuntimeError(
                    f"The stage {scheduler_new.get_stage()} of the new "
                    f"scheduler does not match"
                    f"the stage {ii} of the old scheduler. "
                    f"scheduler, which should not happen"
                )
            for report in old_reports:
                scheduler_new.plan_next_iteration(report)
            if old_stage.complete() and (
                not scheduler_new.stage_schedulers[ii].complete()
            ):
                scheduler_new.force_stage_complete()
        else:
            break
    return scheduler_new


def submit_concurrent_learning(
    wf_config,
    reuse_step: Optional[List[Step]] = None,
    old_style: bool = False,
    replace_scheduler: bool = False,
    no_submission: bool = False,
):
    # normalize args
    wf_config = normalize_args(wf_config)

    global_config_workflow(wf_config)

    dpgen_step = workflow_concurrent_learning(wf_config, old_style=old_style)

    if reuse_step is not None and replace_scheduler:
        scheduler_new = copy.deepcopy(
            dpgen_step.inputs.parameters["exploration_scheduler"].value
        )
        idx_old = get_scheduler_ids(reuse_step)[-1]
        scheduler_old = (
            reuse_step[idx_old].inputs.parameters["exploration_scheduler"].value
        )
        scheduler_new = copy_scheduler_plans(scheduler_new, scheduler_old)
        exploration_report = (
            reuse_step[idx_old].inputs.parameters["exploration_report"].value
        )
        # plan next
        # hack! trajs is set to None...
        conv, lmp_task_grp, selector = scheduler_new.plan_next_iteration(
            exploration_report, trajs=None
        )
        # update output of the scheduler step
        reuse_step[idx_old].modify_output_parameter(
            "converged",
            conv,
        )
        reuse_step[idx_old].modify_output_parameter(
            "exploration_scheduler",
            scheduler_new,
        )
        reuse_step[idx_old].modify_output_parameter(
            "lmp_task_grp",
            lmp_task_grp,
        )
        reuse_step[idx_old].modify_output_parameter(
            "conf_selector",
            selector,
        )

    wf = Workflow(name="dpgen")
    wf.add(dpgen_step)

    # for debug purpose, we may not really submit the wf
    if not no_submission:
        wf.submit(reuse_step=reuse_step)

    return wf


def print_list_steps(
    steps,
):
    ret = []
    for idx, ii in enumerate(steps):
        ret.append(f"{idx:8d}    {ii}")
    return "\n".join(ret)


def successful_step_keys(wf):
    all_step_keys_ = wf.query_keys_of_steps()
    wf_info = wf.query()
    all_step_keys = []
    for ii in all_step_keys_:
        if wf_info.get_step(key=ii)[0]["phase"] == "Succeeded":
            all_step_keys.append(ii)
    return all_step_keys


def get_resubmit_keys(
    wf,
):
    all_step_keys = successful_step_keys(wf)
    all_step_keys = matched_step_key(
        all_step_keys,
        [
            "prep-train",
            "run-train",
            "prep-lmp",
            "run-lmp",
            "select-confs",
            "prep-fp",
            "run-fp",
            "collect-data",
            "scheduler",
            "id",
        ],
    )
    all_step_keys = sort_slice_ops(
        all_step_keys,
        ["run-train", "run-lmp", "run-fp"],
    )
    return all_step_keys


def resubmit_concurrent_learning(
    wf_config,
    wfid,
    list_steps=False,
    reuse=None,
    old_style=False,
    replace_scheduler=False,
):
    wf_config = normalize_args(wf_config)

    global_config_workflow(wf_config)

    old_wf = Workflow(id=wfid)
    all_step_keys = get_resubmit_keys(old_wf)

    if list_steps:
        prt_str = print_keys_in_nice_format(
            all_step_keys,
            ["run-train", "run-lmp", "run-fp"],
        )
        print(prt_str)

    if reuse is None:
        return None
    reuse_idx = expand_idx(reuse)
    reuse_step = []
    old_wf_info = old_wf.query()
    for ii in reuse_idx:
        reuse_step += old_wf_info.get_step(key=all_step_keys[ii])

    wf = submit_concurrent_learning(
        wf_config,
        reuse_step=reuse_step,
        old_style=old_style,
        replace_scheduler=replace_scheduler,
    )

    return wf
