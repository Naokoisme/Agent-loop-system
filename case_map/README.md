# case map / Profile 接入约定

本目录只保留框架接入说明，不保存真实项目用例、外部探索历史或执行证据。

运行具体目标时，由授权的内部 Profile 提供项目专属目录，例如：

```text
case_map/
  <profile-id>/
    <sheet-name>.json
    external_execution_history.jsonl
```

每个项目必须使用自己的 Profile、命令、坐标和证据，禁止跨目标静默回退。真实数据应通过
NAS Profile、内部制品库或受控私有数据仓分发，不应提交到框架源码仓库。
