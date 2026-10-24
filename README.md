<img src="logo.png" width="96" align="right" alt="AnimeTrace">

# 二次元角色识别（AnimeTrace）

引用一张图片，回复 `anime` 就能识别出图中的动漫 / Galgame 角色。

## 用法

```
1. 长按/引用一张带二次元角色的图片
2. 回复：anime
```

也可以**直接把图片和 anime 一起发出**（不需要引用）。

**不需要 @ 机器人，也不需要唤醒前缀** —— 插件监听全部消息自行判断。

## 输出示例

```
ご注文はうさぎですか？ · 保登心愛
    其他可能：Clover Day's · 鷹倉杏鈴 ／ 恋×シンアイ彼女 · 小鞠ゆい
```

图中检测到多个人物时会分行列出（前几个）。若图片疑似 AI 生成，末尾会附加提示。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `trigger_words` | `["anime"]` | 触发词，可填多个 |
| `model` | 空 | 留空则自动取官方当前启用的默认模型。**建议留空** |
| `is_multi` | `true` | 返回多个候选 |
| `ai_detect` | `true` | AI 生成图检测 |
| `max_candidates` | 3 | 每个角色显示几个候选 |
| `max_faces` | 3 | 最多显示几个检测框 |
| `cooldown_seconds` | 5 | 同一会话同一用户的冷却，防刷 |
| `request_timeout` | 40 | 接口超时（秒） |

## 说明

- 数据来源：[AnimeTrace](https://www.animetrace.com/?nav=recognize)（免费接口，无需 Key）
- 接口存在**使用上限**（错误码 `17728`）与**高峰限流**（`17731`），故内置冷却；若频繁触发请调大 `cooldown_seconds`
- 官方提醒"可用模型会动态增减"，因此本插件**不写死模型名**，运行时向 `/v1/model/list` 获取
- 图片读取支持 `http(s)`、`base64://`、`file://` 与本地路径

## 作者与贡献者

| 角色 | 名字 | 说明 |
|---|---|---|
| 作者 / 维护 | **Seira**（セイラ） | 插件设计、开发与维护 |
| 贡献者 | [**月月有狐伴雨眠**](https://github.com/Ruoyuezero) | 需求提出、功能验证、测试与仓库维护 |

欢迎提交 Issue 与 Pull Request，也欢迎在插件市场留下评价 (∗´꒳`∗)
