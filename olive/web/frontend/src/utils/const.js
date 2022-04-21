export const convert_form = {
  conversion_config: '',
  model: null,
  model_framework: '',
  onnx_opset: '',
  model_root_path: '',
  input_names: '',
  output_names: '',
  input_shapes: '',
  input_types: '',
  output_types: '',
  onnx_model_path: 'res.onnx',
  sample_input_data_path: '',
  pytorch_version: '',
  tensorflow_version: '',
};
export const perf_tuning_form = {
  optimization_config: '',
  model: null,
  sample_input_data_path: '',
  input_names: '',
  output_names: '',
  input_shapes: '',
  output_shapes: '',
  providers_list: '',
  trt_fp16_enabled: false,
  openmp_enabled: false,
  quantization_enabled: false,
  transformer_enabled: false,
  transformer_args: '',
  execution_mode_list: '',
  intra_thread_num_list: '',
  inter_thread_num_list: '',
  omp_wait_policy_list: '',
  omp_max_active_levels: '',
  ort_opt_level_list: '',
  concurrency_num: '',
  test_num: '',
  warmup_num: '',
  onnxruntime_version: '1.11.0',
  use_gpu: false,
  throughput_tuning_enabled: false,
  max_latency_percentile: '',
  max_latency_ms: '',
  dynamic_batching_size: '',
  threads_num: '',
  min_duration_sec: '',
};
