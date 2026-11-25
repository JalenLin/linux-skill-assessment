This is an **example multi-node question**: a small Kubernetes cluster built with kubeadm.

| Node | Role | IP |
|---|---|---|
| `cp1` | control plane | 192.168.50.11 |
| `w1` | worker | 192.168.50.21 |
| `w2` | worker | 192.168.50.22 |

Use the tabs above the terminal to switch between machines. `kubectl` works on `cp1` (as `user` too).

`kubectl get nodes` shows that **w2 is NotReady**. Fix it, then:

1. Make node `w2` Ready again
2. Scale the deployment `web` (namespace `default`) to **3** replicas, all of them ready

Hints:

- What runs on every node and reports the node's status to the control plane?
- After a cold start the cluster needs a minute or two before everything is Ready
- The account `user` can use `sudo`
