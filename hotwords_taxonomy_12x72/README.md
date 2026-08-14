# 12×72 分类热词表

`full.txt` 是 C 方案使用的静态热词表，由消防接警 ASR 的12个一级分类、72个二级分类词表合并并全局去重生成。

重新生成：

```bash
python generate_taxonomy_hotwords.py
```

产物：

- `full.txt`：网关握手注入 FunASR 的一行一词表。
- `category_index.csv`：72个二级分类的原始词数、去重贡献数和覆盖状态。
- `manifest.json`：输入及产物校验值。

数字、否定、方言、噪声和多人重叠等不适合静态词汇偏置的分类仍保留在索引中，但不会强行生成热词。原 `hotwords_full/full.txt` 保留，不覆盖，可用于回退。
