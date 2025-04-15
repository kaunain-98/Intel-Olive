# -------------------------------------------------------------------------
# Copyright (c) Intel Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import logging
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Type, Union

from huggingface_hub.constants import HUGGINGFACE_HUB_CACHE

from olive.hardware.accelerator import AcceleratorSpec, Device
from olive.model import CompositeModelHandler, HfModelHandler, OpenVINOModelHandler
from olive.passes import Pass
from olive.passes.pass_config import BasePassConfig, PassConfigParam, get_user_script_data_config

logger = logging.getLogger(__name__)


class OpenVINOOptimumConversion(Pass):
    """Convert a Hugging Face PyTorch model to OpenVINO model using the Optimum export function."""

    @classmethod
    def _default_config(cls, accelerator_spec: AcceleratorSpec) -> Dict[str, PassConfigParam]:
        return {
            **get_user_script_data_config(),
            "components": PassConfigParam(
                type_=List[str],
                default_value=None,
                description=(
                    "List of component models to export. E.g. ['decoder_model', 'decoder_with_past_model']. None means"
                    " export all components."
                ),
            ),
            "device": PassConfigParam(
                type_=Device,
                default_value=accelerator_spec.accelerator_type.CPU,
                description=(
                    "The device to use to do the export. Defaults to 'cpu'."
                    "This is the parameter that is directly passed to Optimum Intel export function in certain cases."
                ),
            ),
            "extra_args": PassConfigParam(
                type_=dict,
                default_value=None,
                description="Extra arguments to pass to the `optimum.exporters.openvino.main_export` function.",
            ),
            "ov_quant_config": PassConfigParam(
                type_=dict,
                default_value=None,
                required=False,
                description=(
                    "Parameters for optimum OpenVINO quantization. Please refer to "
                    "https://huggingface.co/docs/optimum/main/intel/openvino/optimization#4-bit"
                ),
            ),
        }

    @classmethod
    def validate_config(
        cls,
        config: Type[BasePassConfig],
        accelerator_spec: AcceleratorSpec,
    ) -> bool:
        if not super().validate_config(config, accelerator_spec):
            return False

        # validate allowed libraries in extra_args if provided
        allowed_libraries = ["transformers", "diffusers", "timm", "sentence_transformers", "open_clip"]
        if (
            config.extra_args
            and config.extra_args.get("library") is not None
            and config.extra_args.get("library") not in allowed_libraries
        ):
            logger.error(
                "Library %s is not supported. Supported libraries are %s.",
                config.extra_args.get("library"),
                ", ".join(allowed_libraries),
            )
            return False

        # validate allowed frameworks if provided
        allowed_frameworks = ["pt", "tf"]
        if (
            config.extra_args
            and config.extra_args.get("framework") is not None
            and config.extra_args.get("framework") not in allowed_frameworks
        ):
            logger.error(
                "Framework %s is not supported. Supported frameworks are %s.",
                config.extra_args.get("framework"),
                ", ".join(allowed_frameworks),
            )
            return False

        # validate quantization weight_format if provided
        allowed_weight_formats = ["fp32", "fp16", "int8", "int4", "mxfp4", "nf4"]
        if (
            config.ov_quant_config
            and config.ov_quant_config.get("weight_format") is not None
            and config.ov_quant_config.get("weight_format") not in allowed_weight_formats
        ):
            logger.error(
                "Weight format %s is not supported. Supported weight formats are %s.",
                config.ov_quant_config.get("weight_format"),
                ", ".join(allowed_weight_formats),
            )
            return False

        # validate quantization quant_mode if provided
        allowed_quant_modes = ["int8", "f8e4m3", "f8e5m2", "nf4_f8e4m3", "nf4_f8e5m2", "int4_f8e4m3", "int4_f8e5m2"]
        if (
            config.ov_quant_config
            and config.ov_quant_config.get("quant_mode") is not None
            and config.ov_quant_config.get("quant_mode") not in allowed_quant_modes
        ):
            logger.error(
                "Quant mode %s is not supported. Supported quant modes are %s.",
                config.ov_quant_config.get("quant_mode"),
                ", ".join(allowed_quant_modes),
            )
            return False

        # validate backup precisions if provided
        allowed_backup_precisions = ["none", "int8_sym", "int8_asym"]
        if (
            config.ov_quant_config
            and config.ov_quant_config.get("backup_precision") is not None
            and config.ov_quant_config.get("backup_precision") not in allowed_backup_precisions
        ):
            logger.error(
                "Backup precision %s is not supported. Supported backup precisions are %s.",
                config.ov_quant_config.get("backup_precision"),
                ", ".join(allowed_backup_precisions),
            )
            return False

        return True

    def _run_for_config(
        self, model: HfModelHandler, config: Type[BasePassConfig], output_model_path: str
    ) -> Union[OpenVINOModelHandler, CompositeModelHandler]:
        from optimum.exporters.openvino import main_export as export_optimum_intel
        from optimum.exporters.openvino.__main__ import infer_task, maybe_convert_tokenizers, maybe_load_preprocessors
        from optimum.exporters.openvino.utils import save_preprocessors
        from optimum.intel.openvino.configuration import _DEFAULT_4BIT_CONFIG, OVConfig, get_default_int4_config
        from optimum.intel.utils.modeling_utils import _infer_library_from_model_name_or_path

        extra_args = deepcopy(config.extra_args) if config.extra_args else {}
        extra_args.update(
            {
                "device": config.device,
            }
        )

        if model.load_kwargs and "trust_remote_code" not in extra_args:
            extra_args["trust_remote_code"] = model.load_kwargs.trust_remote_code

        if extra_args.get("library") is None:
            lib_name = _infer_library_from_model_name_or_path(model.model_name_or_path)
            if lib_name == "sentence_transformers":
                logger.warning(
                    "Library name is not specified. "
                    "There are multiple possible variants: `sentence_transformers`, `transformers`. "
                    "`transformers` will be selected. "
                    "If you want to load your model with the `sentence-transformers` library instead, "
                    "please set library_name as sentence_transformers"
                )
                lib_name = "transformers"
        else:
            lib_name = extra_args["library"]

        if config.ov_quant_config:
            if config.ov_quant_config.get("weight_format") is None and config.ov_quant_config.get("quant_mode") is None:
                ov_config = None
                if not no_compression_parameter_provided(config.ov_quant_config):
                    raise ValueError(
                        "Some compression parameters are provided, but the weight format is not specified. "
                        "Please provide it with weight_format key in ov_quant_config dictionary."
                    )
                if no_quantization_parameter_provided(config.ov_quant_config):
                    raise ValueError(
                        "Some quantization parameters are provided, but the quant mode is not specified. "
                        "Please provide it with quant_mode key in ov_quant_config dictionary."
                    )
            elif config.ov_quant_config.get("weight_format") in {"fp16", "fp32"}:
                ov_config = OVConfig(dtype=config.ov_quant_config["weight_format"])
            else:
                if config.ov_quant_config.get("weight_format") is not None:
                    # For int4 quantization if no parameter is provided, then use the default config if exists
                    if (
                        no_compression_parameter_provided(config.ov_quant_config)
                        and config.ov_quant_config.get("weight_format") == "int4"
                    ):
                        quant_config = get_default_int4_config(model.model_name_or_path)
                    else:
                        quant_config = prep_wc_config(config.ov_quant_config, _DEFAULT_4BIT_CONFIG)
                    if quant_config.get("dataset", None) is not None:
                        quant_config["trust_remote_code"] = config.ov_quant_config.get("trust_remote_code", False)
                    ov_config = OVConfig(quantization_config=quant_config)
                else:
                    ov_config = None
                    if config.ov_quant_config.get("dataset", None) is None:
                        raise ValueError(
                            "Dataset is required for full quantization. "
                            "Please provide it in ov_quant_config dictionary under 'dataset' key"
                        )
                    if config.ov_quant_config.get("quant_mode") in [
                        "nf4_f8e4m3",
                        "nf4_f8e5m2",
                        "int4_f8e4m3",
                        "int4_f8e5m2",
                    ]:
                        if lib_name == "diffusers":
                            raise NotImplementedError("Mixed precision quantization isn't supported for diffusers.")
                        wc_config = prep_wc_config(config.ov_quant_config, _DEFAULT_4BIT_CONFIG)
                        wc_dtype, q_dtype = config.ov_quant_config["quant_mode"].split("_")
                        wc_config["dtype"] = wc_dtype

                        q_config = prep_q_config(config.ov_quant_config)
                        q_config["dtype"] = q_dtype
                        quant_config = {
                            "weight_quantization_config": wc_config,
                            "full_quantization_config": q_config,
                            "num_samples": self.args.num_samples,
                            "dataset": self.args.dataset,
                            "trust_remote_code": self.args.trust_remote_code,
                        }
                    else:
                        quant_config = prep_q_config(config.ov_quant_config)
                    ov_config = OVConfig(quantization_config=quant_config)
        else:
            ov_config = None

        # quantization config
        quant_config = ov_config.quantization_config if ov_config else None
        quantize_with_dataset = quant_config and getattr(quant_config, "dataset", None) is not None
        task = infer_task(extra_args.get("task", "auto"), model.model_name_or_path, library_name=lib_name)

        # model
        if lib_name == "diffusers" and quantize_with_dataset:
            from diffusers import DiffusionPipeline

            diffusers_config = DiffusionPipeline.load_config(model.model_name_or_path)
            class_name = diffusers_config.get("_class_name", None)

            if class_name == "LatentConsistencyModelPipeline":
                from optimum.intel import OVLatentConsistencyModelPipeline

                model_cls = OVLatentConsistencyModelPipeline

            elif class_name == "StableDiffusionXLPipeline":
                from optimum.intel import OVStableDiffusionXLPipeline

                model_cls = OVStableDiffusionXLPipeline
            elif class_name == "StableDiffusionPipeline":
                from optimum.intel import OVStableDiffusionPipeline

                model_cls = OVStableDiffusionPipeline
            elif class_name == "StableDiffusion3Pipeline":
                from optimum.intel import OVStableDiffusion3Pipeline

                model_cls = OVStableDiffusion3Pipeline
            elif class_name == "FluxPipeline":
                from optimum.intel import OVFluxPipeline

                model_cls = OVFluxPipeline
            elif class_name == "SanaPipeline":
                from optimum.intel import OVSanaPipeline

                model_cls = OVSanaPipeline
            else:
                raise NotImplementedError(f"Quantization isn't supported for class {class_name}.")

            output_model = model_cls.from_pretrained(
                model.model_name_or_path, export=True, quantization_config=quant_config
            )
            output_model.save_pretrained(output_model_path)
            if not extra_args.get("disable_convert_tokenizer", False):
                maybe_convert_tokenizers(lib_name, output_model_path, model, task=task)
        elif (
            quantize_with_dataset and (task.startswith("text-generation") or "automatic-speech-recognition" in task)
        ) or (task == "image-text-to-text" and quant_config is not None):
            if task.startswith("text-generation"):
                from optimum.intel import OVModelForCausalLM

                model_cls = OVModelForCausalLM
            elif task == "image-text-to-text":
                from optimum.intel import OVModelForVisualCausalLM

                model_cls = OVModelForVisualCausalLM
            else:
                from optimum.intel import OVModelForSpeechSeq2Seq

                model_cls = OVModelForSpeechSeq2Seq

            # In this case, to apply quantization an instance of a model class is required
            output_model = model_cls.from_pretrained(
                model.model_name_or_path,
                export=True,
                quantization_config=quant_config,
                stateful=not extra_args.get("disable_stateful", False),
                trust_remote_code=extra_args.get("trust_remote_code", False),
                variant=extra_args.get("variant", None),
                cache_dir=extra_args.get("cache_dir", HUGGINGFACE_HUB_CACHE),
            )
            output_model.save_pretrained(output_model_path)

            preprocessors = maybe_load_preprocessors(
                model.model_name_or_path, trust_remote_code=extra_args.get("trust_remote_code", False)
            )
            save_preprocessors(
                preprocessors, output_model.config, output_model_path, extra_args.get("trust_remote_code", False)
            )
            if not extra_args.get("disable_convert_tokenizer", False):
                maybe_convert_tokenizers(lib_name, output_model_path, preprocessors=preprocessors, task=task)

        else:
            extra_args["ov_config"] = ov_config
            extra_args["stateful"] = not extra_args.get("disable_stateful", False)
            extra_args.pop("disable_stateful", False)
            extra_args["convert_tokenizer"] = not extra_args.get("disable_convert_tokenizer", False)
            extra_args.pop("disable_convert_tokenizer", False)
            extra_args["library_name"] = lib_name
            extra_args.pop("library", None)
            export_optimum_intel(
                model.model_name_or_path,
                output_model_path,
                **extra_args,
            )

        # check the exported components
        exported_models = [name.stem for name in Path(output_model_path).iterdir() if name.suffix == ".xml"]
        if config.components:
            assert all(component in exported_models for component in config.components), (
                f"Components {config['components']} are not exported. Only {exported_models} are exported."
            )
        components = config.components or exported_models
        logger.debug("Exported models are: %s. Returning components: %s.", exported_models, components)

        # if there is only one component, return it directly
        if components is not None and len(components) == 1:
            # will always return an openvino model handler with folder as the model path
            return OpenVINOModelHandler(model_path=output_model_path)

        # if there are multiple components, return a composite model
        model_components = []
        model_component_names = []
        for component_name in components:
            # Note: since conversion is done directly to the output path, all components are in the same folder
            # this is not the same as for other composite models where each component is in a separate subfolder
            model_components.append(
                OpenVINOModelHandler(
                    model_path=output_model_path,
                    model_attributes=model.model_attributes,
                )
            )
            model_component_names.append(component_name)

        return CompositeModelHandler(model_components, model_component_names)


def prep_wc_config(quant_cfg, default_cfg):
    """Prepare the weight compression config for OpenVINO."""
    is_int8 = quant_cfg.get("weight_format") == "int8"
    return {
        "bits": 8 if is_int8 else 4,
        "ratio": 1.0 if is_int8 else (quant_cfg.get("ratio") or default_cfg.get("ratio")),
        "sym": quant_cfg.get("sym", False),
        "group_size": -1 if is_int8 else quant_cfg.get("group_size"),
        "all_layers": None if is_int8 else quant_cfg.get("all_layers", False),
        "dataset": quant_cfg.get("dataset"),
        "num_samples": quant_cfg.get("num_samples"),
        "quant_method": "awq" if quant_cfg.get("awq", False) else "default",
        "sensitivity_metric": quant_cfg.get("sensitivity_metric"),
        "scale_estimation": quant_cfg.get("scale_estimation", None),
        "gptq": quant_cfg.get("gptq", None),
        "lora_correction": quant_cfg.get("lora_correction", None),
        "dtype": quant_cfg.get("weight_format"),
        "backup_precision": quant_cfg.get("backup_precision"),
    }


def prep_q_config(quant_cfg):
    """Prepare the quantization config for OpenVINO."""
    return {
        "dtype": quant_cfg.get("quant_mode"),
        "bits": 8,
        "sym": quant_cfg.get("sym", False),
        "dataset": quant_cfg.get("dataset"),
        "num_samples": quant_cfg.get("num_samples"),
        "smooth_quant_alpha": quant_cfg.get("smooth_quant_alpha"),
        "trust_remote_code": quant_cfg.get("trust_remote_code", False),
    }


def no_compression_parameter_provided(q_config):
    return all(
        it is None
        for it in (
            q_config.get("ratio", None),
            q_config.get("group_size", None),
            q_config.get("sym", None),
            q_config.get("all_layers", None),
            q_config.get("dataset", None),
            q_config.get("num_samples", None),
            q_config.get("awq", None),
            q_config.get("scale_estimation", None),
            q_config.get("gptq", None),
            q_config.get("lora_correction", None),
            q_config.get("sensitivity_metric", None),
            q_config.get("backup_precision", None),
        )
    )


def no_quantization_parameter_provided(q_config):
    return all(
        it is None
        for it in (
            q_config.get("sym", None),
            q_config.get("dataset", None),
            q_config.get("num_samples", None),
            q_config.get("smooth_quant_alpha", None),
        )
    )
