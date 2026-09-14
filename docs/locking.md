# 单实例锁 `astock.locking`

职责：防止同一数据库的两个采集、恢复或导出命令并发操作。输入数据库 Path，输出上下文管理器。

```python
from pathlib import Path
from astock.locking import InstanceLock
with InstanceLock(Path("data/astock.sqlite3")):
    pass
```

以展开并 resolve 的数据库路径加 `.lock` 确定锁路径，父目录自动创建。POSIX 使用非阻塞 flock；Windows 使用 msvcrt 一字节非阻塞锁。冲突抛 `AlreadyRunning(RuntimeError)`。IO 失败传播。无 PID 文件猜测或强删 stale lock：内核在进程退出时释放锁，锁文件本身不删除，避免旧 inode/新 inode 造成双实例。

CLI 的所有五类命令持有同一锁；status/export 也不会与采集并发。嵌入式调用 Store/Scheduler 时调用者必须持锁，Store 单独使用不会自动获得全程进程锁。保证范围是同一规范路径的同机协作进程；不支持多主机共享 SQLite、网络文件系统或用硬链接别名绕过锁。

macOS 已测；Windows 分支有实现但本次环境未做实机验收。
