# 全量 ASR 前处理热词

本目录与 `hotwords/` 动态热词目录相互独立：

- `address.txt`：从两个深圳 AddressBot Excel 的 `name` 列合并生成。
- `slots.txt`：问询槽位名称。
- `full.txt`：地址、槽位及 `hotwords/` 动态词表合并去重后的旧全量握手词表。未设置 `ASR_FULL_HOTWORD_FILE` 时仍作为兼容默认值；当前 C 方案通过环境变量加载 `hotwords_taxonomy_12x72/full.txt`。

重新生成：

```bash
python generate_full_hotwords.py
```

默认合并以下地址表：

- `/home/twai/wjl/AddressBot/data_v4/深圳/AddressBot_shenzhen_with_real_data_selected_duplicates_merged.xlsx`
- `/home/twai/wjl/AddressBot_v3/data_v4/深圳/AddressBot_shenzhen_with_real_data_software_park_phase2_aoi3.xlsx`

需要改用其他地址表时，可以重复传入 `--workbook`。

生成器还会读取原动态目录的以下文件并全部并入 `full.txt`：

- `hotwords/address.txt`
- `hotwords/inquiry_fire_base.txt`
- `hotwords/scenes/highrise.txt`
- `hotwords/scenes/crowded_place.txt`
- `hotwords/scenes/chemical.txt`
- `hotwords/scenes/elevator.txt`

可以用 `--dynamic-dir` 指向其他动态热词目录。原动态词表不会被修改。

生成时删除中英文圆括号、方括号和花括号字符，保留括号内文字，并删除词内空白。

运行模式：

```bash
# 默认：全量词表
ASR_PREPROCESS_HOTWORD_MODE=full

# 备选：原有阶段动态词表
ASR_PREPROCESS_HOTWORD_MODE=dynamic
```

可通过 `ASR_FULL_HOTWORD_FILE` 覆盖 `full.txt` 路径；动态模式仍通过 `ASR_HOTWORD_DIR` 覆盖词表目录。
