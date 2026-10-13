# Ingress Restart Investigation

Date: 2026-07-03

## Conclusion

The ingress controller pods were not restarted by a rollout.

They were repeatedly evicted and rescheduled because worker nodes hit `ephemeral-storage` pressure / `DiskPressure`.

There is no evidence in the collected pod status and event history of a full ingress blackout where zero ingress pods were available at the same time.

The incident shows reduced redundancy, not a complete loss of request routing.

## Availability Impact

No timestamp was found where all ingress controller pods were unavailable.

At least these 4 replicas remained ready throughout the incident window that was investigated:

- `ingress-nginx-controller-7b99cc9f5c-5j564`
- `ingress-nginx-controller-7b99cc9f5c-75tfh`
- `ingress-nginx-controller-7b99cc9f5c-89cbq`
- `ingress-nginx-controller-7b99cc9f5c-rwfq9`

### Lowest Observed Capacity Window

The clearest degraded window is:

- `2026-07-03 06:46:04Z` to `2026-07-03 06:53:41Z`
- `2026-07-03 08:46:04` to `2026-07-03 08:53:41` CEST

During that period:

- `ingress-nginx-controller-7b99cc9f5c-crpbf` had already finished at `06:46:04Z`
- `ingress-nginx-controller-7b99cc9f5c-rzzhv` only became `Ready` at `06:53:41Z`
- the 4 ready replicas listed above were still available

This supports a conclusion of degraded capacity rather than a timeframe with no request routing.

## Current Pod Placement

### `cluster-worker01`

- `ingress-nginx-controller-7b99cc9f5c-4qv2w` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-89hhz` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-ck7v5` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-krplm` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-rzzhv` (`Running`)

### `cluster-worker02`

- `ingress-nginx-controller-7b99cc9f5c-5j564` (`Running`)
- `ingress-nginx-controller-7b99cc9f5c-75tfh` (`Running`)

### `cluster-worker03`

- `ingress-nginx-controller-7b99cc9f5c-kzrcz` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-rwfq9` (`Running`)

### `cluster-worker04`

- `ingress-nginx-controller-7b99cc9f5c-89cbq` (`Running`)
- `ingress-nginx-controller-7b99cc9f5c-npbb6` (`Completed`)

### `cluster-worker05`

- `ingress-nginx-controller-7b99cc9f5c-mhx2f` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-nc57q` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-rq8tc` (`Completed`)

### `cluster-worker06`

- `ingress-nginx-controller-7b99cc9f5c-crpbf` (`Completed`)
- `ingress-nginx-controller-7b99cc9f5c-jbzt9` (`Completed`)

### Active Replicas

The 5 active ingress controller replicas are currently running on:

- `cluster-worker01`
- `cluster-worker02` (2 replicas)
- `cluster-worker03`
- `cluster-worker04`

## Primary Evidence

- Namespace events in `logs/events.txt` show repeated `Evicted` events for ingress controller pods:
  - `The node was low on resource: ephemeral-storage`
  - `The node had condition: [DiskPressure]`
- Replacement pods also show `FailedScheduling` before landing, because some candidate nodes had:
  - untolerated taint `node.kubernetes.io/disk-pressure`
  - too many pods
  - node affinity / selector mismatch
- `logs/node-cluster-worker05.describe.txt` shows `DiskPressure=True` at capture time.
- `logs/node-cluster-worker01.describe.txt` and `logs/node-cluster-worker02.describe.txt` show recent `DiskPressure` transitions back to `False`.

## Important Deployment Detail

- `logs/deployment-ingress-nginx-controller.yaml` defines CPU and memory requests only.
- There is no `ephemeral-storage` request for the controller container.
- During eviction, Kubernetes reports `request is 0` in `logs/events.txt`.

## Why Pods Appear as `Completed`

Several terminated ingress pods show:

- `State: Terminated`
- `Reason: Completed`
- `Exit Code: 0`

This does not indicate a normal batch completion. In this case the pods were evicted, then the controller process exited cleanly during termination.

## Scheduling Constraints Observed

- The controller is limited by `nodeSelector`:
  - `environment=shared`
  - `kubernetes.io/os=linux`
- Scheduler messages also mention:
  - 26 nodes did not match node affinity / selector
  - 3 control-plane nodes were tainted
  - 3 to 4 nodes had too many pods

That reduced placement options while some worker nodes were under disk pressure.

## Artifacts Collected

- `logs/pods-wide.txt`
- `logs/events.txt`
- `logs/workloads.txt`
- `logs/deployment-ingress-nginx-controller.yaml`
- one `.yaml`, `.describe.txt`, `.log`, and `.previous.log` file per ingress pod
- `logs/node-cluster-worker01.describe.txt`
- `logs/node-cluster-worker02.describe.txt`
- `logs/node-cluster-worker05.describe.txt`

## Notable Limitation

Some terminated pod log files contain:

- `unable to retrieve container logs for containerd://...`

That means container runtime logs for some older evicted pods were already unavailable at collection time.
