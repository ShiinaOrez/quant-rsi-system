# Tinker checkpoint 本地部署

核查日期：2026-10-14。结论：可以离开 Tinker 做本地推理。当前项目没有训练后的 checkpoint，因此这里只确认官方导出路径，尚未实测导出模型。

Tinker 的 sampler checkpoint 可下载，通常是 LoRA adapter，不包含完整底模。用于继续训练的 state 与用于推理的 sampler weights 应分别保存。[官方 checkpoint 文档](https://tinker-docs.thinkingmachines.ai/tinker/howto/checkpoints/)

两条官方路径：转换为 PEFT adapter 后由支持 LoRA 的运行时加载；或合并对应底模，得到普通 Hugging Face 权重。[PEFT 转换](https://tinker-docs.thinkingmachines.ai/cookbook/deployment/lora-adapter/)、[合并导出](https://tinker-docs.thinkingmachines.ai/cookbook/deployment/export-hf/)

推荐本项目 Mac 路径：sampler checkpoint → 合并 HF 权重 → GGUF → Q4_K_M → Ollama。运行时支持和转换后的数值一致性仍需用实际 checkpoint 验证。[Ollama GGUF 导入](https://docs.ollama.com/import)、[llama.cpp 量化工具](https://github.com/ggml-org/llama.cpp/tree/master/tools/quantize)

导出示例（在单独的训练/导出环境安装并锁定 tinker-cookbook；不要把占位路径当成已有模型）：

```python
from tinker_cookbook import weights

adapter_dir = weights.download(
    tinker_path="tinker://<run_id>/sampler_weights/final",
    output_dir="./adapter",
)
weights.build_hf_model(
    base_model="/absolute/path/to/exact-base-model-snapshot",
    adapter_path=adapter_dir,
    output_path="./merged_model",
    merge_strategy="shard",
)
```

底模必须与训练时一致：模型 ID、HF revision、tokenizer 和 chat template 都要归档。Tinker 内部张量命名需要官方转换；不能直接把下载的 adapter 加到任意 Ollama 量化底模。先合并再量化可减少这种适配问题。

当前本地 `qwen3:8b` 是通用后训练模型的量化运行副本。它不能作为 Tinker 浮点采样的等价数值基线：应分别记录训练 revision、导出 revision、量化方式、运行时版本和提示模板。正式对比需在同一运行时、量化方式与采样配置下重新评估所有候选。

32 GB M1 Pro 可以运行本次 8B 量化推理；HF 合并所需内存取决于分片和工具实现，建议分片合并或在训练机器导出。下载 adapter 后的本地推理不必逐次调用 Tinker，但底模许可证和数据许可证仍适用。

本地轨迹可支持行为分析和生成 SFT 候选；本次题目是 validation split，不能作为训练输入。当前 Ollama 接口未保存 token IDs 与行为策略 logprobs，轨迹标记 `rl_training_ready=false`，不能直接冒充 PPO/on-policy RL 数据。后续 RL 优先在 Tinker 采样，或增加经过验证的 token/logprob 采样适配器。
