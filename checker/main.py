import typer
import itertools
import pandas as pd
from rich import print
from tabulate import tabulate
from typing import List, Optional, Dict
from typing_extensions import Annotated
from dataclasses import dataclass
from kubernetes import config, client
from kubernetes.client.models.v1_pod import V1Pod
from kubernetes.client.api.core_v1_api import CoreV1Api
from kubernetes.client.models.v1_container import V1Container


def normalize_cpu_unit(val: str) -> int:
    """
    CPU's can be given as:
    - Integer: 1, 2, 3...
    - Float: 0.5, 0.1...
    - Millicpu/millicore: 100m, 500m

    Examples: 
    - 1 CPU is equvalent to 1000m.
    - 0.6 CPU is equvalent to 600m.

    In this function, we return all given values as millicores
    converting them from floats or integers if necessary.
    """
    if "m" in val:
        millicores = int(val[:-1]) # remove m from the end and return
        return millicores
    return int(float(val) * 1000)


def normalize_mem_unit(val: str) -> float:
    """
    Memory is measured in bytes. However, it can be expressed as:
    - Integer: 1128974848
    - Decimal Exponent: 12e6
    - With quantity suffixes E, P, T, G, M, K: 12M  (12 * 10^6)
    - Or with power of 2 equivalent Ei, Pi, Ti, Gi, Mi, Ki: 12Mi (12 * 2^20)

    In this function, we convert and return all values with suffix M.
    """
    suffixes = {
        "K": 10**3,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "P": 10**15,
        "E": 10**18,
        "Ki": 2**10,
        "Mi": 2**20,
        "Gi": 2**30,
        "Ti": 2**40,
        "Pi": 2**50,
        "Ei": 2**60,
    }
    return_sfx_mult = suffixes["Mi"]

    if "e" in val: # if decimal exponent
        return float(val) / return_sfx_mult

    for suffix, multiplier in suffixes.items(): # if a suffix is used
        length = len(suffix)
        if suffix == val[-1 * length:]:
            val_wo_sfx = float(val[:-1 * length])
            val_in_bytes = val_wo_sfx * multiplier
            return val_in_bytes / return_sfx_mult

    return float(val) / return_sfx_mult # if given as bytes in integer


def get_resource_value(
    cont: V1Container, def_type: str, res_type: str
) -> Optional[str]:
    if cont.resources is None:
       return None
    definition = getattr(cont.resources, def_type)
    if definition is None:
       return None
    if res_type not in definition:
       return None
    return definition[res_type]


@dataclass
class Container:
    name: Optional[str]
    cpu_request: Optional[int]
    cpu_limit: Optional[int]
    mem_request: Optional[float]
    mem_limit: Optional[float]

    @staticmethod
    def from_api(cont: V1Container) -> 'Container':
        name = "Undefined" if cont.name is None else cont.name
        cpu_req = get_resource_value(cont, "requests", "cpu")
        if cpu_req is not None:
            cpu_req = normalize_cpu_unit(cpu_req)
        cpu_lim = get_resource_value(cont, "limits", "cpu")
        if cpu_lim is not None:
            cpu_lim = normalize_cpu_unit(cpu_lim)
        mem_req = get_resource_value(cont, "requests", "memory")
        if mem_req is not None:
            mem_req = normalize_mem_unit(mem_req)
        mem_lim = get_resource_value(cont, "limits", "memory")
        if mem_lim is not None:
            mem_lim = normalize_mem_unit(mem_lim)
        return Container(name, cpu_req, cpu_lim, mem_req, mem_lim)


@dataclass
class Pod:
    name: str
    namespace: str
    status: str
    node: str
    containers: List[Container]

    @staticmethod
    def from_api(pod: V1Pod) -> 'Pod':
        # Note: Assumes all attributes are defined for the pods since
        # they are mandatory (for running container?)
        name = pod.metadata.name
        namespace = pod.metadata.namespace
        status = pod.status.phase
        node = pod.spec.node_name
        containers = [Container.from_api(cont) for cont in pod.spec.containers]
        return Pod(name, namespace, status, node, containers)


def get_api() -> CoreV1Api:
    config.load_kube_config()
    api = client.CoreV1Api()
    return api


def get_namespaces() -> List[str]:
    api = get_api()
    namespace_list = api.list_namespace()
    return [
        namespace.metadata.name 
        for namespace in namespace_list.items 
        if namespace.metadata
    ]


def get_pods(namespace: str) -> List[Pod]:
    api = get_api()
    pod_list = api.list_namespaced_pod(namespace=namespace)
    pods = [Pod.from_api(pod) for pod in pod_list.items]
    return pods


@dataclass
class Row:
    node: str
    namespace: str
    name: str
    status: str
    cpu_req: Optional[int]
    cpu_lim: Optional[int]
    mem_req: Optional[float]
    mem_lim: Optional[float]
    # uncertain* flags indicate whether the pod has resource req/lim
    # undefined, or one of the containers in the pod has resource req/lim
    # undefined. Therefore, they can consume more mem/cpu then what is
    # lister.
    cpu_req_undef: bool
    cpu_lim_undef: bool
    mem_req_undef: bool
    mem_lim_undef: bool

    @staticmethod
    def from_pod(pod: Pod) -> 'Row':
        node = pod.node
        namespace = pod.namespace
        name = pod.name
        status = pod.status
        cpu_req, cpu_lim, mem_req, mem_lim = 0, 0, 0.0, 0.0
        cpu_req_undef, cpu_lim_undef = False, False
        mem_req_undef, mem_lim_undef = False, False
        for container in pod.containers:
            cpu_req_undef = cpu_req_undef or (container.cpu_request is None)
            cpu_lim_undef = cpu_lim_undef or (container.cpu_limit is None)
            mem_req_undef = mem_req_undef or (container.mem_request is None)
            mem_lim_undef = mem_lim_undef or (container.mem_limit is None)
            cpu_req += int(container.cpu_request or 0)
            cpu_lim += int(container.cpu_limit or 0)
            mem_req += float(container.mem_request or 0)
            mem_lim += float(container.mem_limit or 0)
        return Row(
            node, namespace, name, status,
            cpu_req, cpu_lim, mem_req, mem_lim,
            cpu_req_undef, cpu_lim_undef,
            mem_req_undef, mem_lim_undef,
        )


def extract_data() -> pd.DataFrame:
    namespaces = get_namespaces()
    pod_lists = [get_pods(namespace) for namespace in namespaces]
    pods = list(itertools.chain.from_iterable(pod_lists))
    rows = [Row.from_pod(pod) for pod in pods]
    df = pd.DataFrame(rows)
    return df


def split_per_node(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    nodes = df.node.unique().tolist()
    return {
        node: df[df['node'] == node].drop(columns=["node"]) 
        for node in nodes
    }


def split_per_namespace(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    namespaces = df.namespace.unique().tolist()
    return {
        namespace: df[df['namespace'] == namespace].drop(columns=["namespace"]) 
        for namespace in namespaces
    }


def summarize_data(df: pd.DataFrame) -> pd.DataFrame:
    sum_cpu_req = df.cpu_req.sum()
    sum_cpu_lim = df.cpu_lim.sum()
    sum_mem_req = df.mem_req.sum()
    sum_mem_lim = df.mem_lim.sum()
    min_cpu_res = not len(df[df["cpu_req_undef"] == True]) > 0 # noqa: E712
    cpu_hard_limit = not len(df[df["cpu_lim_undef"] ==  True]) > 0 # noqa: E712
    min_mem_res = not len(df[df["mem_req_undef"] == True]) > 0 # noqa: E712
    mem_hard_limit = not len(df[df["mem_lim_undef"] == True]) > 0 # noqa: E712
    return pd.DataFrame(
        [[
            sum_cpu_req, sum_cpu_lim, sum_mem_req, sum_mem_lim, 
            min_cpu_res, cpu_hard_limit,
            min_mem_res, mem_hard_limit,
        ]],
        columns=[
            'Total CPU Req (m)', 'Total CPU Lim (m)',
            'Total Mem Req (MiB)', 'Total Mem Lim (MiB)',
            'Min CPU reserved?', 'CPU hard limit?',
            'Min Mem reserved?', 'Mem hard limit?',
        ]
    )


def tabulate_data(df: pd.DataFrame, summary_df: pd.DataFrame):
    df = df.rename(columns={
        'name': 'Name',
        'node': 'Node',
        'namespace': 'Namespace',
        'status': 'Status',
        'cpu_req': 'CPU Req (m)',
        'cpu_lim': 'CPU Lim (m)',
        'mem_req': 'Mem Req (MiB)',
        'mem_lim': 'Mem Lim (MiB)',
        'cpu_req_undef': 'CPU Req Undef',
        'cpu_lim_undef': 'CPU Lim Undef',
        'mem_req_undef': 'Mem Req Undef',
        'mem_lim_undef': 'Mem Lim Undef',
    })
    print(tabulate(df, headers='keys', tablefmt='psql', maxcolwidths=20))
    print(tabulate(summary_df, headers='keys', tablefmt='psql', showindex=False))


def df_has_row(df: pd.DataFrame, msg: str) -> bool:
    if len(df) == 0:
        print(msg)
        return False
    return True


def main(
    per_node: Annotated[
        bool, typer.Option(
            help="Group results by node. Can't be used together with --per-namespace.", 
            show_default=True,
    )] = False,
    per_namespace: Annotated[
        bool, typer.Option(
            help="Group results by namespace. Can't be used together with --per-node.", 
            show_default=True,
    )] = False,
    node: Annotated[
        Optional[str], typer.Option(
            help="Filter results by given node name.", 
            show_default=True,
    )] = None,
    namespace: Annotated[
        Optional[str], typer.Option(
            help="Filter results by given namespace.", 
            show_default=True,
    )] = None,
    status: Annotated[
        Optional[str], typer.Option(
            help="Filter results by given status.", 
            show_default=True,
    )] = "Running",
):
    """
    List resource requests/limits defined for the pods on k8s cluster
    accessible via local KUBECONFIG.
    """
    if per_node and per_namespace:
        print(
            "[yellow]:warning: Both --per-node and --per-namespace "
            "options are enabled. Defaulting to --per-node.[/yellow]"
        )
        per_namespace = False
    no_row_msg = "[bold red]:exclamation: No pod found! Stopping...[/bold red]"

    print("[green]:hourglass_flowing_sand: Extracting data from the cluster...[/green]")
    df = extract_data()

    if not df_has_row(df, no_row_msg):
        raise typer.Exit(1)

    if node is not None:
        print(f"[purple]:computer: Filtering by node '{node}'...[/purple]")
        df = df[df['node'] == node]
        if not df_has_row(df, no_row_msg):
            raise typer.Exit(1)

    if namespace is not None:
        print(f"[purple]:blue_book: Filtering by namespace '{namespace}'...[/purple]")
        df = df[df['namespace'] == namespace]
        if not df_has_row(df, no_row_msg):
            raise typer.Exit(1)

    if status is not None:
        print(f"[purple]:running: Filtering by status '{status}'...[/purple]")
        df = df[df['status'] == status]
        if not df_has_row(df, no_row_msg):
            raise typer.Exit(1)

    if per_node:
        print("[purple]:flags: Grouping by node...[/purple]")
        per_node_dfs = split_per_node(df)
        print("[green]:rocket: Tabulating the data...[/green]")
        for node, node_df in per_node_dfs.items():
            msg = f"\n\n[yellow]:warning: No pod found in {node}! Skipping...[/yellow]"
            if not df_has_row(node_df, msg):
                continue
            print("\n\n")
            print("[yellow]"+ "="*30 + f" Node: {node} [/yellow]\n")
            tabulate_data(node_df, summarize_data(node_df))
    elif per_namespace:
        print("[purple]:flags: Grouping by namespace...[/purple]")
        per_namespace_dfs = split_per_namespace(df)
        print("[green]:rocket: Tabulating the data...[/green]")
        for namespace, namespace_df in per_namespace_dfs.items():
            msg = f"\n\n[yellow]:warning: No pod found in {namespace}! Skipping...[/yellow]"
            if not df_has_row(namespace_df, msg):
                continue
            print("\n\n")
            print("[yellow]"+ "="*30 + f" Namespace: {namespace} [/yellow]\n")
            tabulate_data(namespace_df, summarize_data(namespace_df))
    else:
        if not df_has_row(df, no_row_msg):
            raise typer.Exit(1)
        print("[green]:rocket: Tabulating the data...[/green]")
        tabulate_data(df, summarize_data(df))



if __name__ == "__main__":
    typer.run(main)
