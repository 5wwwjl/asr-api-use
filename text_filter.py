"""
文本过滤 — 噪声词识别 & 有效文本判断。

与 VAD 完全解耦：VAD 只做音频级人声检测，文本级过滤在这里。
"""

NOISE_WORDS: set[str] = {
    # 单字语气词
    "嗯", "啊", "哦", "呃", "额", "唔", "噢", "诶", "哎", "唉", "呀", "哟", "哈",
    "呐", "嘛", "呢", "吧", "啦", "嗨", "嘿", "哼", "咦", "啧", "呵",

    # 重复语气词
    "嗯嗯", "啊啊", "哦哦", "呃呃", "额额", "唔唔", "噢噢",
    "诶诶", "哎哎", "唉唉", "呀呀", "哈哈", "呵呵", "嘿嘿",
    "嗯啊", "啊嗯", "嗯哼", "哼嗯",

    # 试麦/呼叫类
    "喂", "喂喂", "喂喂喂", "喂你好", "你好喂",
    "听得到吗", "能听到吗", "听得到", "听不听得到",

    # 低价值确认词，单独出现时可过滤
    "好", "好的", "好好", "嗯好", "哦好", "行", "行行",
    "可以", "可以可以", "对", "对对", "是", "是是",

    # FunASR/麦克风噪声容易误出的短词
    "可", "了", "的", "是的", "这个", "那个", "然后",
    "就是", "那个那个", "这个这个",

    # 背景声/笑声/咳嗽拟声，视场景过滤
    "咳", "咳咳", "咳嗽", "哈哈哈", "呵呵呵",
}


def is_noise_text(text: str, noise_words: set[str] | None = None) -> bool:
    """判断文本是否为噪声词——不含有效语义信息。"""
    nw = noise_words or NOISE_WORDS
    cleaned = text.strip().replace(" ", "").replace("\n", "")
    return cleaned in nw or len(cleaned) <= 1


def has_valid_content(text: str, noise_words: set[str] | None = None) -> bool:
    """判断文本是否包含有效语义内容（非空且非纯噪声）。"""
    return bool(text) and not is_noise_text(text, noise_words)


def filter_noise_segments(
    segments: list[str], noise_words: set[str] | None = None
) -> list[str]:
    """过滤文本片段列表，去掉纯噪声项。"""
    return [s for s in segments if has_valid_content(s, noise_words)]


def clean_noise_fillers(
    text: str, noise_words: set[str] | None = None
) -> str:
    """去除文本中嵌入的口语填充词（那个、嗯、就是、对...），保留有效内容。

    与 is_noise_text 不同，此函数处理句子内部的口语噪声，
    如 "那个...北京市朝阳区..." → "北京市朝阳区"。

    Args:
        text: 含口语噪声的原始文本
        noise_words: 自定义噪声词集合，默认使用 NOISE_WORDS

    Returns:
        清洗后的文本
    """
    import re
    nw = noise_words or NOISE_WORDS
    # Sort by length descending to match longer phrases first
    sorted_noise = sorted(nw, key=len, reverse=True)
    result = text
    for word in sorted_noise:
        # Match noise word when surrounded by non-word boundaries
        # (not part of a larger meaningful word)
        pattern = (
            r'(?:^|[,，。、；;：:\s…\.\-—]+)'
            + re.escape(word)
            + r'(?:$|[,，。、；;：:\s…\.\-—]+)'
        )
        result = re.sub(pattern, '', result)
    # Collapse multiple punctuation/spaces
    result = re.sub(r'[,，。、；;：:\s…\.\-—]{2,}', '', result)
    # Remove leading/trailing punctuation and whitespace
    result = result.strip('，,。、；;：: \t\n….-—')
    return result
