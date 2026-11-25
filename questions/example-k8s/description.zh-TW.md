這是一題**多節點範例題**：用 kubeadm 建好的小型 Kubernetes 叢集。

| 節點 | 角色 | IP |
|---|---|---|
| `cp1` | control plane | 192.168.50.11 |
| `w1` | worker | 192.168.50.21 |
| `w2` | worker | 192.168.50.22 |

用終端機上方的分頁切換機器。`cp1` 上可以直接用 `kubectl`（`user` 也可以）。

`kubectl get nodes` 顯示 **w2 是 NotReady**。請修好它，並且：

1. 讓節點 `w2` 回到 Ready
2. 把 deployment `web`（namespace `default`）調整成 **3** 個副本，而且全部就緒

提示：

- 每個節點上都有哪個服務負責把節點狀態回報給 control plane？
- 叢集剛開機需要一兩分鐘，所有元件才會就緒
- 帳號 `user` 可以使用 `sudo`
