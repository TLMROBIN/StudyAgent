# 侧栏折叠性能验证

2026-10-12，针对 `frontend/src/styles.css` 两处已有的布局属性动画告警：侧栏 width/padding 和主区域 padding-left。

侧栏尺寸和主区域留白现在一次切换到最终布局，不再逐帧改变答疑工作区宽度。折叠功能、状态记忆、自动窄屏折叠、按钮反馈和系统减少动态效果设置保留；没有新增动画或告警忽略。

## 验证

同一台开发电脑、Chrome headless、1440×900、同一学生页面 API fixture，CDP Performance 指标，连续切换六次，每次等待 300ms：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| LayoutCount | 122 | 6 |
| LayoutDuration | 30.192ms | 3.000ms |
| RecalcStyleCount | 132 | 13 |
| RecalcStyleDuration | 16.276ms | 5.195ms |

重排次数降低约 95.1%。这是浏览器局部交互测量，不代表模型回答加速或真实课堂平板的帧率。

- 1440×900、768×1024、390×844：无横向页面溢出，窄屏自动折叠，侧栏宽度分别为 288/76/68px。
- Enter 键切换、刷新后折叠状态保留、连续十次快速切换、系统 reduced-motion：通过。
- 前端生产构建、`git diff --check`：通过。
- 本地测量与截图：`artifacts/diagnostics/sidebar-optimization/`（未提交的诊断证据）。

两个告警均修复；没有抑制项，没有留存的本次告警。其他界面设计不在本次改动范围。
