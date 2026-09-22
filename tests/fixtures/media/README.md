# 真实媒体样本

此目录中的媒体默认不提交 Git。仅使用有权限的真实视频，覆盖静态 PPT、翻页、
屏幕文字、多人对话、运动与断流重连。不要下载用户素材或合成模型成功结果。

运行 `uv run python tools/probe_media.py /absolute/path/to/video.mp4` 可生成真实文件的
SHA-256、音视频流信息与时长报告。报告不包含原始路径，不代表模型或 Golden Path 验收。
