[English](../README.md) | 简体中文

# pfs2bos

使用 rclone 将已经停止写入的模型、数据和实验产物复制到 BOS，逐文件验证后，将原文件原子替换为指向 BOS 挂载的绝对软链接。Python 3.10+，安装时自动安装 `python-dotenv`；需要 Linux、Git 和已配置的 rclone。

映射示例（路径需自行配置）：

```text
/mnt/pfs/workspace/RMBench/data/example.parquet
  → bos:your-bucket/artifacts/RMBench/data/example.parquet
原路径软链接 → /mnt/bos/artifacts/RMBench/data/example.parquet
```

## 运行

先安装到当前 Python 环境或虚拟环境：

```bash
cd /mnt/pfs/workspace
python3 -m pip install --user ./tools/pfs2bos
cp ./tools/pfs2bos/example.env .env
# 编辑 .env，填写三个配置值后再运行
pfs2bos scan --path RMBench --out plan.json
```

从 GitHub 克隆仓库后，可在仓库根目录执行 `python3 -m pip install .`；虚拟环境中省略 `--user`。也支持 `python3 -m pfs2bos`。扫描输出实时文件计数；清单包括 `entries`（选中文件）、`pending`（未知类型，仅待确认）、排除计数和预计释放的已分配字节数。每项记录相对路径、识别理由、目标、大小、设备号、inode、mtime/ctime 纳秒、链接数和权限。

检查生成的清单，停止训练、下载及其他写入任务，确认挂载可读后手动执行：

```bash
pfs2bos apply --plan plan.json
pfs2bos status --plan plan.json
pfs2bos status --plan plan.json --verify
pfs2bos restore --plan plan.json
```

`apply` 默认 4 路上传，可用 `--transfers 2` 调整。`status` 输出 JSON，核对当前路径形态；加 `--verify` 会完整读取、计算 SHA-256 并核对已记录内容。它不会访问 rclone 远端，也不会修改清单。失败返回 1，正常返回 0，Ctrl-C 返回 130。

先用一个很小、已停止写入的产物目录执行上述完整流程，确认原路径读取正常、内容一致、`du` 的本地占用下降，再扩大范围。首次测试可先 `restore` 验证回退。工具不会自动发起真实迁移。

## 选择范围

`scan` 默认读取**运行命令时所在目录**的 `.env`，不向上搜索父目录。可复制仓库的 `example.env` 为 `.env` 并修改：

```dotenv
PFS2BOS_ROOT=/mnt/pfs/workspace
PFS2BOS_REMOTE=bos:your-bucket/artifacts
PFS2BOS_MOUNT=/mnt/bos/artifacts
```

从其他目录运行时，可以明确指定文件：

```bash
pfs2bos scan --env-file /mnt/pfs/workspace/tools/pfs2bos/.env --path RMBench --out plan.json
```

优先级为命令行 `--root` / `--remote` / `--mount` > 已有环境变量 > `.env`，没有内置默认值。扫描必须有 `.env`（或 `--env-file` 指定的文件）；文件不存在时会提示复制 `example.env` 并填写，即使提供了命令行参数或环境变量也不例外。最终配置值缺失或为空时同样停止运行并提示填写。值按字面读取，不展开 `${VAR}`。`.env` 已加入 Git 忽略规则，仓库只提交 `example.env`。

这些设置只影响新清单的扫描；`apply`、`status`、`restore` 始终读取清单中保存的映射。

`--path` 是相对于配置的工作区根目录的项目目录，可重复指定。不提供则扫描整个根目录。扩展名默认包括权重、Parquet/Arrow/NumPy 等数据、图片、音视频、压缩包；没有最小大小限制。未知扩展名和普通 JSON/索引等文件进入 `pending`，不会上传。

将配套元数据一起迁移时，明确指定数据目录：

```bash
pfs2bos scan --path RMBench --data-dir RMBench/data --out rmbench-plan.json
```

或通过配置：

```json
{
  "data_dirs": ["RMBench/data", "RMBench/outputs/frozen-run"],
  "exclude_dirs": ["RMBench/outputs/active-run"]
}
```

```bash
pfs2bos scan --path RMBench --config config.json --out rmbench-plan.json
```

配置目录同样相对于工作区根目录，按目录边界匹配。明确数据目录包含其所有普通文件，但仍遵守排除规则；请勿将项目根目录随意列为数据目录。需要调整范围时生成新清单，不要手动把 `pending` 移入 `entries`。

跳过 `.git`、快照目录、常见虚拟环境及 `pyvenv.cfg`/`conda-meta` 标识的环境、miniforge、缓存、开发工具配置、`bos-cli`、`tools` 和工具自身的临时/锁文件。跳过 Git 跟踪文件（含祖先仓库和嵌套仓库）、现有软链接、特殊文件、多硬链接文件，不跟随目录软链接。Git 查询失败会中止扫描。含换行的文件名保留在 `pending`，首版不迁移；支持中文、空格和 `#` 文件名。

可用 `--root`、`--remote`、`--mount` 覆盖扫描时的映射，后续命令从清单读取这些参数；挂载路径必须与源树分离。清单固定处理范围，`apply` 不会纳入扫描后新出现的文件。清单是迁移日志，请保留到恢复结束。

## 校验、冲突和中断

1. `apply` 核对扫描时的文件身份并读取 SHA-256，将待上传状态持久化。
2. 使用仅含清单文件的 `rclone copy --files-from-raw ... --ignore-existing --no-traverse` 上传。现有同名对象不覆盖，不修改远端目录中的其他文件。整个上传批次失败时，本次待处理文件均保留本地副本，之后可重跑。
3. 每个文件使用 `rclone check --download --one-way` 校验真实内容。现有同名同内容对象复用；内容不同会记录校验失败并保留源文件。大小一致也必须通过内容校验，不依赖 ETag。工具不会自动解决冲突或覆盖对象。
4. 再从挂载路径完整读取并比对 SHA-256，重新读取源文件并核对其身份。挂载不可读、内容不一致或源文件变化，均保留本地文件。
5. 持久化 `linking` 状态，在原目录建立临时软链接，最后一次核对身份后使用 `os.replace` 切换并 fsync 目录，然后记录 `linked`。每个阶段原子保存并 fsync 清单。

校验会增加 BOS 读取流量和耗时，每个文件都要读取远端、挂载和本地内容。逐文件校验优先隔离失败，目录含大量小文件时会有进程启动开销。

错误按文件记录到 `error`，其他可验证文件仍可继续。解决挂载或网络问题后，重跑原 `apply` 即可。若源文件变化，生成新清单。`linking` 状态可能表示切换前或切换后中断，重跑会核对实际路径并继续。每个源根目录使用 `.pfs2bos.lock` 排他锁，防止不同清单并发操作同一个根目录；进程退出后锁自动释放，锁文件可以保留。

`restore` 从 rclone 远端下载到原目录的临时文件，核对原 SHA-256、恢复权限和 mtime、fsync 后原子替换工具创建的软链接。下载失败或内容变化会保留原链接。恢复前后的中断均可重跑 `restore`；已恢复的文件不会再次覆盖。BOS 副本始终保留；恢复后的文件要再次迁移，请重新扫描。

## 边界

必须先停止写入。身份/哈希检测无法排除最后检查与 rename 之间的并发写入，也无法阻止其他客户端在验证后修改远端；工具的锁仅协调本工具。源文件切换后，原路径写入会沿软链接写到 BOS。读取性能和可用性取决于挂载。

保留开发环境、代码与工具文件，不承诺工作区只剩代码。空间估计使用 `st_blocks * 512`，未扣除软链接、目录和日志开销；PFS 的延迟分配可能使尚未刷盘的文件被低估，快照或打开的文件句柄仍可能保留底层占用。恢复需要足够的本地空间，不恢复原 atime、所有者、ACL 或扩展属性。

工具只做复制，不删除远端对象。`--ignore-existing` 防止覆盖已有对象，但无法与外部上传者实现对象级事务；运行期间应避免其他程序写同一 BOS 前缀。清单和源目录应由可信用户控制。

## 开发与验证

代码采用 `src/pfs2bos/` 包布局：

- `cli.py`：参数解析、命令分发与退出码。
- `scan.py`、`config.py`：扫描、Git 跟踪检查和选择规则。
- `migrate.py`：rclone 复制、内容校验、软链接切换和恢复。
- `plan.py`：清单校验和持久化阶段记录。
- `filesystem.py`：路径检查、文件身份、哈希和锁。
- `status.py`、`errors.py`：状态核对与公共异常。

在仓库根目录，先安装开发版本再运行测试：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试直接导入已安装包，并调用安装生成的 `pfs2bos` 命令，不修改 `sys.path`。发布前也应安装构建出的 wheel，运行相同测试，确认打包包含所有模块。

测试在自动清理的临时目录中使用真实 rclone 本地后端，覆盖路径保留、中文/空格、空文件、排除规则、Git 跟踪、同名等长内容冲突、上传/校验/挂载故障、源文件变化、链接与恢复切换前后中断、重跑、锁及恢复保护。不会使用真实 BOS 凭证或迁移已有模型/数据；BOS 与挂载的端到端验收需在手动小规模试跑时完成。
