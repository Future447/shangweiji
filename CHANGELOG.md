# Changelog

## 2026-09-16 — v3.3.0（通道物理位置映射：热力图与实际线圈接线一致）

### 最终接线布局
- 4 列从左到右、每列自下而上编号：第 1 列 CH01–CH04（下→上），
  第 2 列 CH05–CH08，第 3 列 CH09–CH12，第 4 列 CH13–CH16
- 热力图自上而下：CH04 CH08 CH12 CH16 / CH03 CH07 CH11 CH15 /
  CH02 CH06 CH10 CH14 / CH01 CH05 CH09 CH13
  （row=0 为顶行，col=0 为左列；公式 row=3−ch%4，col=ch//4）
- 左下角单元格 = CH01（内部通道 0），右上角 = CH16（内部通道 15）；
  点击/键盘方向键均按屏幕物理相邻位置选中通道

### 映射来源唯一化（core/channel_layout.py，新模块）
- HARDWARE_CHANNEL_MAP / LEGACY_ROW_MAJOR_MAP / 严格校验
  （必须 16 通道、16 个唯一在界坐标；缺失/重叠/越界/格式错误
  一律显式拒绝，禁止静默补全或产生重叠）/ CSV 紧凑格式
  序列化与解析，全部集中在一处
- 消除 6 处各自为政的行优先默认：config_manager 默认值与
  getter 回退、HeatmapWidget 初始化默认与 set_channel_map 静默
  clamp/丢弃/补全、MainWindow._apply_channel_map_to_heatmap 兜底、
  DemoSource.DEFAULT_MAP_4x4、MainWindow._start_demo 未传映射——
  现统一引用 channel_layout
- 设置对话框"通道"页启用网格同步按物理位置摆放；
  default_enabled_4ch/PRESET_4CH 声明修正为"左下 2×2"
  （CH01/CH02/CH05/CH06，通道号索引不变）

### 只改位置、不重排数据
- samples_mv[i] / valid_mask bit i / 处理器 / 滤波 / 基线 /
  启用通道 / CSV ch01_* 列全部保持通道号索引；衰减量与标定
  逻辑不变；单元格整体移动（值+标签+位置同源），不调换文字标签

### 旧配置一次性迁移（独立 mapping_version，不 bump CONFIG_VERSION）
- 存储映射完全匹配旧行优先默认 → 迁移前备份配置文件
  （config.json.premap_v2.bak）→ 写入实际接线布局与
  mapping_version=2 并保存；重启后布局保持、不重复迁移；
  真实基线与基线来源信息、串口/滤波/实验参数全部保留
- 缺失映射 → 新默认；非法映射 → 显式回退硬件默认并提示；
  合法自定义映射 → 保留不覆盖，设置对话框提供显式
  "恢复硬件默认布局"入口（确认后随保存生效）

### 演示与记录/回放同步
- DemoSource 创建与更新均传入当前生效映射（按 channel 0..15
  顺序构造 map_to_grid）：演示目标在左下角附近时 CH01 响应最大，
  右上角附近时 CH16 响应最大
- 新 CSV 的 channel_map 列记录记录时的生效映射（测量列顺序不变）
- 回放：CSV 含有效记录映射时默认按"记录时布局"显示（消息栏与
  日志明确标注仅位置不同）；无映射信息的历史文件按当前硬件布局
  显示并明确告知（不假称恢复历史物理位置）；回放临时映射绝不
  写入实时配置；退出回放（或换载无需临时映射的文件）后恢复
  当前硬件布局；历史 CSV 文件不被修改

### 测试
- 新增 tests/test_channel_map.py（20 项）：布局矩阵、值/标签/位置
  三元组、点击与键盘物理导航、布局变更前后数据语义不变、旧配置
  迁移与重启保持、自定义映射保留与恢复入口、演示物理响应、
  CSV 映射记录与回放恢复
- test_config_recorder / test_csv_v4 / test_p0_regressions 同步更新
  （非法映射不再静默补全、按通道查物理位置）
- 全部 14 个测试文件 312 项通过；verify_layout_v2 六尺寸通过，
  channel_map 检查新增 bottom_left_is_ch01 / top_right_is_ch16 /
  matches_hardware_layout 全 True

### 对抗复审二次修复（6 维度审查 → 10 项确认发现）
- channel_layout.normalize：显式捕获 OverflowError——json 可解析
  1e999/Infinity 为 inf，int(inf) 旧实现会让含损坏映射的配置
  在启动迁移时直接崩溃；现显式丢弃该项并走非法映射回退
- config_manager 迁移：channel_map 键存在但值畸形（null/空表/
  字符串）时，旧实现只补 mapping_version 标记却不修复数据，
  畸形值永久落盘且不再提示；现显式写回硬件默认 + 备份 + 提示
  文案区分"缺少映射"与"格式非法"
- 回放状态一致性三处：
  · 退出回放时撤下"按记录时布局显示"消息栏标注（回到实时
    模式后该现在时断言已为假，不得残留）
  · live→replay 往返且文件仍加载时，重新进入回放即恢复该
    文件的"记录时布局"显示并重新标注（旧实现静默按当前
    接线布局显示历史数据）；状态栏区分"待加载/已加载"
  · 回放中应用设置（如仅改 EMA alpha）后重放记录映射——
    旧实现会把热力图静默切回硬件布局而标志位仍为 True，
    显示/标志/消息栏三者失同步
- 换载"记录布局与当前一致"的文件时，旧"记录时布局"标注随
  临时布局一并撤下，不残留
- verify_layout_v2 键盘导航检查：更新过期注释（旧行优先期望
  idx 1）并补显式期望断言（新布局 Right→CH05=idx 4、Down 越界
  保持 4），导航退化不再静默通过
- 测试加固：布局变更前后基线断言改为 16 通道互异非 None 基线
  （旧断言 None==None 无鉴别力）+ 滤波状态逐通道等值断言；
  回放"明确标注"要求补消息栏可见性与文案断言；新增畸形映射
  修复落盘、inf 坐标不崩溃、回放中应用设置三项回归

## 2026-09-16 — v3.2.0（实用性审查修复批次：测量正确性与基线可靠性）

针对实用性审查发现的 5 项问题修复，再经 6 维度对抗复审
（13 项确认发现）二次修复。

### P1 饱和数据污染滤波器和标定
- `core/channel_processor.py` ingest 重排：非有限值/饱和 rail 值
  在进中值/EMA 之前判出——饱和帧保留真实 rail 采样作诊断
  （状态列明确标记 SATURATED），但绝不进入滤波链与标定累计
  （旧实现先 `update_mv` 后判饱和：2 帧 rail + 5 帧 1000 mV 会
  标定出 2981.360 mV 的假基线）
- `models/channel_state.py` 新增 `ChannelStatus.WARMING_UP`：
  中值窗未填满期间滤波值不代表当前，快照 filtered/attenuation
  为 None，UI 显示"预热中"；长中断（>2 s 无接纳样本）后自动
  重新播种滤波并按本通道有效样本重新预热（短中断直接续用）

### P1 标定漂移与末段稳定性门槛
- `core/calibration_manager.py` 新增五道门（阈值集中、带单位
  与依据）：cv_mad、前后半段中位数漂移、异常点比例（门限
  max(5×1.4826×MAD, 5 mV)——小样本量化聚簇下 MAD 会低估
  离散度，5 mV 下限 ≈ 前端噪声 ±3σ）、末段 cv、末段中位偏移；
  MAD=0 的台阶信号（15×1000+5×800）现在必然失败并点名
  通道/指标/阈值/重标建议；失败/取消保留旧基线，基线成功后
  固定不自动追踪
- 整体超时随目标样本数放大（×0.35 s/样本）——旧恒 180 s 使
  UI 可选的大目标在实机帧率下数学上必然失败；卡死早判帧数
  加上限 150（旧 4×200=800 帧 ≈ 229 s 超出 180 s 预算，默认
  参数下早判分支为死代码）

### P1 局部重新标定的基线同步
- 重标定成功后未参与通道基线失效（既定策略）：processor
  `apply_calibration` 清空其基线并把 OK 降级为"未标定"；
  config 按参与通道集合整体替换；全部校验通过才提交（原子）
- 基线落盘失败：弹窗告知并停止进行中的记录（避免用旧
  baseline_id 标注新基线下的数据）

### P2 回放曲线使用实验原始时间
- `ui/plot_widget.py` add_point/add_attenuation_point 接受显式
  时间戳：回放用 row.t_rel（t=0 与 t=60 的行在图上相距 60 s，
  与播放速度无关），实时用会话相对接收时间（一帧所有曲线
  共用同一时间戳）；时间倒退（seek 回退）清窗重建，绝不
  倒序连线；缺失/非有限时间用内部单调时钟兜底；暂停不加点
- `core/replay_source.py` 调度修复：索引未前进不重复发行
  （旧实现低速播放时同一行按墙钟重复加点，可视窗被压缩）；
  多行同时到期逐行全发（旧实现被跨过的中间行永远不进曲线）；
  非有限 t_rel（nan/inf）加载时修复为"上一有效时间+中位行距"
  （旧实现调度比较恒 False，回放永久冻结在坏行之前）

### P2 无效帧的显示一致性
- `core/measurement.py` 统一有效性门槛：sample_for_state /
  filtered_for_state；无效/饱和/预热帧的 filtered/attenuation/
  drop_pct 一律 None（UI "—"、CSV 留空、曲线 NaN 断点——
  不跳过、不连线、不显示残留值）；饱和帧保留 rail 诊断采样；
  回放分支同样受 frame_valid 门控（旧 CSV 的残留滤波值不得
  显示成当前有效值）；基线作为历史参数保留显示
- 详情面板各字段直接采用快照值（旧实现叠加第二道门槛，
  预热/饱和时 tooltip 有真实采样而面板显示 "—"）；首帧即
  饱和的通道曲线画 rail 诊断点（旧实现 sample_count=0 整条
  曲线空白）

### 对抗复审二次修复
- 运行中缩小中值窗立即生效（旧实现缓冲卡在旧长度，新配置
  静默不生效）；标定提交不得把 WARMING_UP 提升为 OK；
  回放模式标定按钮保持禁用且 `_on_calib_clicked` 拒绝启动
  （旧实现切回放 cancel 的收尾槽会重新启用按钮，可启动永远
  收不到帧的幻影标定）；回放模式热力图自动色阶跟随回放
  衰减量（旧实现 frame_valid 缺省值不回放感知，色阶冻结）

### 测试（291 passed，按文件分批运行）
- 新增 tests/test_practicality_fixes.py(36)：饱和不污染（用户
  复现 2981→恰好 1000.000）、五道标定门（台阶/缓变/稳定/
  单尖峰/基线固定/失败保旧）、局部重标定主窗口集成
  （processor/config/UI/CSV 全链路与 baseline_id 一致性）、
  回放 t_rel X 轴（60 s 间隔/速度不变性/暂停/回退重建/残留
  不显示）、无效帧端到端（processor/详情/tooltip/曲线/CSV
  一致 + 饱和帧 CSV 诊断列）、复审回归锁 12 项
- tests/test_smoke.py：TestDemoModeBoot 注入临时配置文件——
  全部 Qt 测试不再读写用户真实基线（此前 demo scope 会写
  真实 config.json）
- tools/verify_layout_v2.py：新增 1366×768 与 1920×1080 验证
  （六种尺寸全部通过，离屏逻辑分辨率）

## 2026-09-16 — v3.1.1（对抗性代码评审修复批次）

对 v3.1.0 做多维对抗评审（29 项确认缺陷），按根因聚类修复：

### 数据真实性（面板/曲线/CSV 不得自相矛盾）
- `core/measurement.py` 新增 `legacy_delta_for_state()`：旧 Δ 与
  衰减量同一有效性门槛（饱和/无效帧一律未知）；详情面板与 CSV
  共用此单一入口——修复"衰减列留空而旧 Δ 列照写"的矛盾行
- 曲线（ui/plot_widget.py）：`add_point` 接受 None → NaN 断点
  （`connect="finite"`），不再伪造 0 mV 直线；t/raw/filt 三条
  序列窗口淘汰同步裁剪（旧实现按当前点是否为 None 决定 pop，
  序列错位后 setData 长度不一致崩溃）；未启用通道（sample_count=0）
  不再画点
- 回放（core/replay_source.py）：wire/filtered/baseline/delta 等
  空单元格一律解析为 None（未知），绝不默认为 0.0；播放中加载
  新文件立即停止并复位时间轴/索引；回放帧曲线与实时同门槛
  （raw_valid 无效不画点）；基线参考线随回放帧同步
- 主窗口：进入/重载回放时清理处理器会话数据、帧计数器与旧
  基线参考线；停止回放保持 REPLAY 源状态（按钮排除逻辑不失效）

### 标定与基线
- `_median` 偶数样本取两中值平均（旧取上中位值，基线系统性
  偏高半个间距）；MAD 同此入口
- scan_number 回绕/设备重启（scan 变小）自动清空去重缓存——
  旧实现把新循环的帧当重复帧吞掉，标定永远凑不齐样本
- 标定提交只保存参与通道（未参与通道旧基线不再被打上新
  baseline_id）；`config.save()` 失败时弹窗告知且不把 CSV 会话
  元数据更新为磁盘上不存在的 baseline_id
- 演示模式【已放入钢板】状态禁止开始无钢板标定；标定期间锁定
  钢板切换按钮；设置对话框在标定期间禁止打开；设置变更后演示源
  跟随新启用通道集
- 旧基线备份跨保存/加载周期保留（旧实现一次保存后备份永久
  丢失）；版本/单位不符文件里的 baseline_sets 清空、备份并标记
  待确认（旧实现被 d.update 静默沿用，绕过作废语义）

### 界面一致性（完整/紧凑两套操作栏）
- 端口列表同步刷新两个下拉；连接以可见操作栏的端口选择为准
  （修复紧凑栏"连接"读了隐藏下拉的空端口）
- 两个记录按钮经 `_sync_rec_buttons()` 同状态源；记录启动失败
  补捕 RuntimeError；切换数据源（新会话）先停止当前记录——
  同一 CSV 不再混入不同来源/会话元数据的帧
- 串口 connected/error/lost/stopped 四槽加 sender 过期防护
  （旧 worker 延迟信号不再污染新会话）
- `_set_source_state` 尾镜像紧凑栏按钮可用性；回放模式下
  `_on_refresh` 永远禁用"开始标定"
- 通道映射（channel_map）启动时与设置变更后应用到热力图；详情
  面板显示所选通道的阵列物理位置（RnCn）
- 热力图单元格经 ShortcutOverride 保住 Space/Enter 选择键，
  不被全局暂停快捷键抢占

### 测试（254 passed，按文件分批运行）
- 新增 tests/test_plot_widget.py(3)：None 断点 / 序列淘汰等长 /
  衰减断点
- test_calibration_flow.py(25)：偶数中位数、scan 回绕去重恢复
- test_baseline_store.py(20)：备份跨保存/加载保留、版本不符
  baseline_sets 作废
- test_csv_v4.py(15)：饱和通道旧 Δ 列同门槛留空、回放空
  wire/filtered/baseline → None
- test_replay_source.py(12)：播放中加载复位、v3 空单元格 → None
- test_smoke.py(14)：回放模式标定禁用、放板状态拦截标定、
  切换来源停止记录、双记录按钮同步

## 2026-09-16 — v3.1.0（无钢板标定 → 放钢板衰减 实验语义整改）

### 实验语义与统一公式
- 新增 `core/measurement.py`：统一测量快照与唯一公式入口
  （详情/热力图/tooltip/曲线/CSV 同源）
  - `attenuation_mv = baseline_mv − filtered_mv`（相对无钢板基线的
    衰减量；正=信号下降，负=信号升高，如实保留负值）
  - `detector_drop_pct = attenuation / baseline × 100`，仅在基线为
    正且 ≥ 1.0 mV（`MIN_VALID_BASELINE_MV`，数值有效性下限，非硬件
    合格标准）时给出，否则 "—"；绝不产生 NaN/Inf/巨大伪百分比
  - 公式版本标识 `attenuation_mv=baseline_mv-filtered_mv@v1` 写入 CSV
- 旧 `delta = filtered − baseline` 保留为旧语义/诊断量；invert_sign
  只作用于旧诊断量，绝不再翻转 attenuation_mv

### 无钢板基线标定（core/calibration_manager.py 重写）
- 操作更名"无钢板基线标定"，开始前弹窗确认"请移除钢板，保持线圈
  位置、激励频率和增益不变"
- 帧驱动 + 同 scan 去重 + 会话门（session_id）+ 前 2 帧滤波预热
  （`WARMUP_FRAMES=2`，只预热不计数）
- 剔除无效/非有限/饱和（触量程边界）/非当前会话样本
- 基线 = 滤波值中位数；质量报告在原始有效样本域给出噪声
  （std/MAD）、漂移（后半均值−前半均值）与异常点
- 稳定门限集中为 `STABILITY_MAX_CV_MAD=0.05`（无量纲
  MAD/|median|，软件自检门限，修复"注释 0.05、代码 0.5"分叉）
- 卡死早判（`STUCK_FRAME_FACTOR×target_frames`，下限 30 帧）与
  停滞/整体超时，失败原因明确列出通道
- 原子提交；取消/失败/断连保留旧基线；成功后提示"无钢板基线已
  建立，请放入钢板测量"；基线固定，不加自动追踪

### 基线按来源隔离（core/config_manager.py，CONFIG_VERSION=4）
- `baseline_sets` 按 `serial`/`demo` 作用域保存：演示标定不覆盖
  硬件基线；回放基线不会写成当前硬件基线
- meta 记录来源、设备标识（USB VID:PID+序列号，不只看 COM 口）、
  单位、通道、时间、ADC/检波配置、频率、增益、baseline_id、公式版本
- 旧扁平无来源基线：加载时备份到 `legacy_baselines_backup` 并标记
  "待确认/需重新标定"，绝不静默沿用
- 切换来源时清理滤波/当前帧有效性/曲线会话数据，串口与演示回调
  带 sender 过期防护

### 界面
- 通道详情主显示：相对基线衰减量（mV）+ 检波电压下降比例（%）；
  基线/滤波/串口采样/VOUT 估算/旧Δ/更新时间为次级（注明物理节点）
- 热力图：标题注明"相对无钢板基线的衰减量；正值：信号下降；负值：
  信号升高"；零点中性色；未标定/无效/饱和/断流显示状态而非正常零值；
  固定/自动色阶并存
- 曲线：保留原始+滤波电压曲线，新增基线参考线（InfiniteLine）与
  衰减量视图（零参考线）；重新标定后派生衰减曲线清空，不用新基线
  静默重算旧数据
- 修复 `ui/heatmap_widget.py` 单元格初始化代码被 stranded 在
  `delta_value` 属性 `return` 之后的缺陷（尺寸/焦点策略/无障碍描述
  恢复生效）

### 演示数据（core/demo_source.py）
- 物理模型 `sample = air_baseline − metal_response + drift + noise`；
  默认【无钢板】；"放入钢板/移除钢板"按钮切换状态；放钢板电压下降、
  移除回到基线附近；响应幅度非负；0.125 mV 量化/限幅仅作用于模拟
  数据；始终标注"模拟数据"，不模拟串口连接成功

### CSV schema v4（core/recorder.py / core/replay_source.py）
- 每通道新增 `chXX_attenuation_mv`、`chXX_detector_drop_pct`；尾部
  新增 `attenuation_formula`、`baseline_source`、`baseline_id`、
  `baseline_device_id`；旧 delta 列语义不变
- 当前帧无效/饱和/断流 → 派生列留空（不使用残留值）；缺失量显示
  未知不默认为 0
- 回放支持 v3/v4：v3 衰减量=None（未知）；v4 保存值直通，不二次
  翻转；v2 明确拒绝；回放首个数据行不再被 schema 检测吞掉
- `update_session_meta()`：重新标定后只影响之后写入的行

### 测试（235 passed，按文件分批运行）
- 新增 test_measurement.py(31) / test_calibration_flow.py(23) /
  test_baseline_store.py(18) / test_demo_flow.py(8) / test_csv_v4.py(13)
- 更新 test_config_recorder.py / test_replay_source.py /
  test_p0_regressions.py / test_smoke.py 适配 warmup、schema v4 与
  无钢板确认弹窗

## 2026-08-31 — v2.1.0 (P0 整改)

### P0-1 标定帧驱动
- `core/calibration_manager.py` 重写：取消 QTimer 60ms 重复读取同一 filtered 的实现
- 新接口 `on_frame(dframe)` 由主窗口在每个 D 帧到达时调用
- 同一 scan_number 帧去重，绝不重复计数
- 仅当 `valid_mask=1` 时该通道样本才累加
- 引入 `target_frames` 参数：要求至少 N 个不同 D 帧
- 新增 watchdog：`max_idle_s`（默认 10s）内无新 D 帧即失败
- 基线采用中位数 + MAD 质量门限；不稳定基线拒绝并保留旧值
- 进度信号现在带 `(current, target, frames, target_frames)`

### P0-2 16 通道记录真实性
- `core/channel_processor.py` 始终记录所有 16 通道的 wire raw（无论是否启用）
- `core/recorder.py` 新增 `write_dframe(dframe, processor, ...)` 接口
  - 每通道 `wire_raw` 直接来自当前 DFrame
  - `valid_mask=0` 通道写空值，不冒充本帧数据
  - 新增 `raw_valid`（0/1）列明确区分本帧有效性
  - 新增 `chXX_delta`（无 invert）与 `chXX_delta_signed`（应用 invert）两列
- CSV 字段重命名为 `wire_raw`（替代旧 `raw`），确保未被旧调用方误用
- 每帧 `flush + fsync`（低帧率下保证断电数据不丢）
- 写入失败自动停止记录并 UI 弹窗
- 增加可追溯元数据：`schema_version`, `app_version`, `source`, `session_id`,
  `enabled_channels`, `channel_map`, `invert_sign`, `vref_mv`

### P0-3 互斥采集会话
- `ui/main_window.py` 重构为 `_start_new_session(source)` 入口
- 启动新数据源（串口/演示）前先停止旧数据源
- 新会话重置：解析器残包、帧/丢帧统计、scan 期望值、帧率窗口、故障判定
- 串口打开失败时 UI 恢复到"未连接"状态，可再次连接
- 断连/切源/串口错误时统一停止标定与记录

### P0-4 故障判定
- 区分三类超时：
  - `no_bytes`：超过 5s 未收到任何字节
  - `parse`：会话开始 5s 后仍有字节但无合法 D 帧（即使从未收到过 D 帧）
  - `stale`：曾收到 D 帧但 5s 无新帧
- 状态机基于 `_session_start_time` 而非 `_last_valid_data_time`
- E 帧 (`init_status != 0`) 标记为"设备故障"，收到健康 D 帧后自动清除
- 同一持续故障不会反复弹窗（依赖 `_data_timeout_reported` flag）

### P0-5 数据语义统一
- `core/channel_processor.py` 保留 `delta_signed()` 单一入口
- `ui/main_window.py` 热力图与 CSV 均通过 `delta_signed()` 读取
- `core/recorder.py` 在写 CSV 时根据 `invert_sign` 计算 `delta_signed`
- 切源/切 Profile 不会改变显示方向（除非用户显式修改 invert_sign）

### 架构调整
- `ui/main_window.py` 拆出 `_start_new_session()`、`_stop_all_sources()` 等辅助方法
- 帧率从"1/距上一帧时间"改为滑动窗口 `(N-1)/Δt`
- 关闭事件中按序停止所有资源
- 新增 `_compute_fps()`、`_write_csv_dframe()` 等职责明确的方法

### 测试
- 新增 `tests/test_p0_regressions.py`（13 个 P0 回归测试）
- 旧测试 `test_columns_match_data`、`test_progress_signals` 更新到新接口
- v2 测试 91 个，全部通过：
  - `test_protocol_parser.py` 27 个
  - `test_config_recorder.py` 8 个
  - `test_channel_processor.py` 21 个
  - `test_smoke.py` 6 个（Qt offscreen 演示 + 串口线程）
  - `test_p0_regressions.py` 13 个
- 旧的 `tests/test_core.py`（227 个旧版测试）从 v2 测试统计中隔离

### CSV Schema 升级
- `SCHEMA_VERSION` 从 1 升到 2
- 每行新增列：`schema_version`, `app_version`, `source`, `session_id`,
  `enabled_channels`, `channel_map`, `vref_mv`, `invert_sign`
- 通道列重命名：`raw` → `wire_raw`，新增 `raw_valid`
- 新增 delta 双轨列：`chXX_delta`（无 invert） + `chXX_delta_signed`（应用 invert）

## 2026-08-31 — v2.0.0 (重写)

### 适配 STM32 固件 v2
- 协议：解析 `D,<scan>,<status>,<mask>,<i2c>,S1..S16\r\n` 数据帧
  和 `E,INIT,...\r\n` 错误心跳（基于 `v2/Core/Src/main.c` 实际
  `USB_SendReceiverFrame` 和 `USB_SendReceiverErrorStatus` 实现）。
- 16 通道全部按 int16 有符号解析（不再按旧 12-bit 0..4095）。
- 不再发送 `SINGLE` / `ARRAY` 模式命令（v2 固件已不实现）。
- 新增丢帧统计（基于 `scan_number` 差值）。
- "无数据"（无字节）与"协议不匹配"（有字节但无 D 帧）分别用
  红色和橙色显示。

### 架构重构
- `core/` 业务层：协议解析、串口 worker、通道处理、标定、记录、配置、演示。
- `models/` 数据层：DFrame / EFrame / ChannelState / AdcProfile。
- `ui/` 界面层：主窗口 / 4×4 热力图 / 选中通道面板 / 实时曲线 / 设置对话框。
- `main.py` 仅做启动；`main_window.py` 只做 UI 装配。

### 4×4 热力图
- 16 格固定 4×4；未启用格灰色。
- 颜色：蓝-白-红对称编码 Δ（滤波 − 基线）。
- 选中通道：黄色边框。
- 自动范围防尖峰（缓慢扩展，立即收缩）。
- 状态颜色：未启用 / 未标定 / 本帧无效 / 饱和 / 正常。

### 阵列空气标定
- 任意样本数；可取消；失败保留旧基线。
- 进度显示在状态栏。
- 标定过程中 GUI 不卡死（QTimer 60ms 驱动）。

### ADC Profile
- 内置 `ads1115_4v096_div2`（默认）和 `stm32_adc12`（兼容）。
- 切换 Profile 后所有通道的 Vadc / AD8362 VOUT 换算自动更新。

### 实时曲线
- 默认只画选中通道。
- 同时画 raw + 滤波。
- 暂停/清空按钮。
- 时间窗口可配置（默认 30 s，60s 滑动缓存）。

### CSV 记录
- 16 通道统一列（即使未启用也保留）。
- UTF-8 BOM，Excel 直接打开。
- 独占创建，文件名不覆盖。
- 程序退出时自动 flush + close。

### 配置
- 版本号 + 字段校验。
- 损坏配置回退到默认值。
- 沿用原项目 `%LOCALAPPDATA%` 配置目录。

### 演示模式
- 模拟金属目标在 4×4 上移动。
- 可配置噪声 σ、漂移、尖峰概率。
- 共用同一套 ChannelProcessor 链路。

### 测试
- 新增 4 个 pytest 文件：协议解析 / 通道处理 / 配置记录 / Qt 烟雾。
- 共 305 个测试，全部通过。

### 打包
- 新 `build_exe.spec` 输出 `涡流阵列采集系统_v2.exe`。
- GUI 模式（不显示控制台）。
- 输出到 `dist_v2/`。
- 原 `dist/涡流传感器采集系统.exe` 保留。

### 保留
- 沿用原项目 `app_paths.py`、`config_manager.py`、`experiment_recorder.py`、
  `serial_handler.py` 的设计思路与对外接口（在新代码中分别重写为
  `core/config_manager.py`、`core/recorder.py`、`core/serial_worker.py`，
  兼容原接口语义但移除 SINGLE/ARRAY 模式命令逻辑）。

---

## 2026-09-02 — v2.1.1 (UI 重构)

### ui/theme.py（新增）
- 集中式主题令牌：`Palette` dataclass + `Metrics` + 字体族
- `build_stylesheet(extra)` 输出 QSS，覆盖 QMainWindow、QLabel、
  QFrame#SurfaceCard、QFrame#StatusStrip、QLabel#StatusBadge、QLabel#Caption、
  QLabel#SectionTitle、QPushButton[role=primary|secondary|danger|ghost]、
  QCheckBox、QComboBox、QGroupBox、QTabBar、QSplitter、QScrollBar、
  QDockWidget、QPlainTextEdit
- `set_status_role(widget, role)` 用于状态语义色（neutral/ok/warning/danger/demo）
- 冷蓝单主色（#2F6FCE），亮色画布，灰阶对比

### ui/components.py（新增）
- `StatusBadge` 圆角小徽标
- `ElidingLabel` 中间省略号（路径展示）
- `SectionHeader` 分区标题 + caption
- `KpiMetric` 紧凑指标（标题 + 等宽数字）
- `SurfaceCard` 圆角卡片容器

### ui/heatmap_widget.py（重写）
- 单元格常态显示通道号 + 主 delta；异常时显示明确文字
- 选中用 3px 冷蓝外框 + 1px 内白色描边（红蓝背景上都清楚）
- SATURATED 用危险边框 + 文字 + 斜线纹理
- NO_DATA / INVALID / NOT_CALIBRATED / DISABLED 通过文字和明度区分
- 单元格 tooltip：raw/filtered/baseline/delta/Vadc/VOUT/valid/最后更新
- 键盘方向键 + Enter / Space 选中
- 新增 `HeatmapLegend` 水平色阶图例

### ui/channel_panel.py（重写）
- 主 delta 用 26pt 等宽粗体；百分比紧邻
- raw/filtered/baseline/Vadc/VOUT/最后更新 用 2×3 KpiMetric 网格
- 头部含通道号、物理位置、状态徽标
- 无数据时显示空状态"—"，不显示看似真实的 0

### ui/plot_widget.py（重写）
- 标题栏：raw/filtered CheckBox 可见性、暂停/清空按钮
- raw：低对比细线；filtered：冷蓝粗虚线
- pyqtgraph 主题统一：背景、网格、坐标轴、字体
- 暂停时显示"曲线已暂停"覆盖层

### ui/settings_dialog.py（重写为 4 页签）
- 采集：ADC Profile + 说明 + Vref
- 通道：4×4 网格 + 当前 4 通道 / 全部通道 / 清空 快捷选择
- 处理与显示：EMA / 中值 / invert_sign / 热力图范围 / 曲线窗口
- 实验信息：频率/单位/增益/线圈/铁板/备注
- 保存/取消固定在底部
- result_values 键名与旧实现兼容

### ui/main_window.py（重写）
- 默认 1360×840，最小 1100×720
- 顶部状态条：状态徽标 + FPS + 有效/无效/丢帧 + 记录徽标
- 操作带：端口 + 刷新 + 波特率 + 连接/断开/演示 + 记录 + 选目录 + 路径省略
- 65:35 水平分割：左 4×4 热力图 + 范围控制 + 色阶图例 + 标定，
  右上通道详情 + 右下曲线（无标签）
- QDockWidget 容纳"日志 / 原始数据"，默认折叠
- 全部按钮使用 `QStyle.standardIcon`
- 全部样式走 `build_stylesheet()` + `set_status_role()`
- P0-1 ~ P0-5 整改行为完整保留

### main.py
- 移除内联 QSS
- 改用 `app.setStyleSheet(build_stylesheet())` + 统一字体

### 验证
- `tests/test_smoke.py` 6/6 通过
- `tests/test_p0_regressions.py` 13/13 通过
- 其余 v2 测试 91/91 通过
- `python -m compileall ui core models main.py` 无错误
- `MainWindow` 全部测试引用属性仍存在
