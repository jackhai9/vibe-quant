<!-- Input: YAML 配置文件与环境变量 -->
<!-- Output: AppConfig/MergedSymbolConfig -->
<!-- Pos: src/config 模块说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# src/config 目录说明

YAML 配置加载与模型验证。<br>
支持 global 默认 + symbol 覆盖合并（含执行反馈参数）。<br>
对外提供合并后的配置对象。

## 文件清单

- `loader.py`：配置加载与合并逻辑
- `models.py`：pydantic 配置模型
- `__init__.py`：模块导出
