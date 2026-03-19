#!/usr/bin/env python3
import ssl
import sys
import atexit
import argparse
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("-H", "--host", required=True, help="vCenter host/IP")
    p.add_argument("-u", "--user", required=True, help="vCenter username")
    p.add_argument("-p", "--password", required=True, help="vCenter password")
    p.add_argument("-v", "--vm", required=True, help="VM name (exact match)")
    p.add_argument("-w", "--warning", type=int, default=0, help="WARNING if files >= N (0 disables)")
    p.add_argument("-c", "--critical", type=int, default=40, help="CRITICAL if files > N")
    p.add_argument("--recursive", action="store_true", help="Recurse subfolders (default: only VM folder)")
    p.add_argument("--snaponly", action="store_true",
                   help="Count only snapshot-like files (*-0000*.vmdk, *.vmsn, *.vmsd, *.delta.vmdk)")
    return p.parse_args()

def find_vm_by_name(content, name: str):
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        for obj in view.view:
            if obj.name == name:
                return obj
    finally:
        view.Destroy()
    return None

def parse_vm_path(vm_path_name: str):
    # Example: "[datastore1] folder/vm.vmx"
    if not vm_path_name or not vm_path_name.startswith("[") or "]" not in vm_path_name:
        raise ValueError(f"Unexpected vmPathName format: {vm_path_name!r}")
    ds = vm_path_name[1:vm_path_name.index("]")]
    rest = vm_path_name[vm_path_name.index("]")+1:].strip()  # "folder/vm.vmx"
    if "/" not in rest:
        # VMX at datastore root
        folder = ""
    else:
        folder = rest.rsplit("/", 1)[0]  # "folder"
    return ds, folder

def wait_task(task):
    while True:
        state = task.info.state
        if state == vim.TaskInfo.State.success:
            return task.info.result
        if state == vim.TaskInfo.State.error:
            raise task.info.error
        # ESXi/vCenter tasks update quickly; no need for long sleep
        import time
        time.sleep(0.2)

def main():
    args = get_args()

    context = ssl._create_unverified_context()
    si = SmartConnect(host=args.host, user=args.user, pwd=args.password, sslContext=context)
    atexit.register(Disconnect, si)

    content = si.RetrieveContent()
    vm = find_vm_by_name(content, args.vm)
    if not vm:
        print(f"UNKNOWN - VM '{args.vm}' not found")
        sys.exit(3)

    if not vm.config or not vm.config.files or not vm.config.files.vmPathName:
        print(f"UNKNOWN - VM '{args.vm}' has no vmPathName")
        sys.exit(3)

    try:
        ds_name, folder = parse_vm_path(vm.config.files.vmPathName)
    except Exception as e:
        print(f"UNKNOWN - cannot parse vmPathName: {e}")
        sys.exit(3)

    # Resolve datastore object by name among VM datastores
    ds_obj = None
    for ds in vm.datastore:
        if ds.summary and ds.summary.name == ds_name:
            ds_obj = ds
            break
    if not ds_obj:
        # fallback: search globally
        for ds in content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True).view:
            if ds.summary and ds.summary.name == ds_name:
                ds_obj = ds
                break
    if not ds_obj:
        print(f"UNKNOWN - datastore '{ds_name}' not found for VM '{args.vm}'")
        sys.exit(3)

    browser = ds_obj.browser

    # Build datastore path to VM folder
    if folder:
        ds_path = f"[{ds_name}] {folder}"
    else:
        ds_path = f"[{ds_name}]"

    spec = vim.HostDatastoreBrowserSearchSpec()
    if args.snaponly:
        spec.matchPattern = ["*-0000*.vmdk", "*.vmsn", "*.vmsd", "*.delta.vmdk"]
    else:
        spec.matchPattern = ["*.vmdk"]

    try:
        if args.recursive:
            task = browser.SearchDatastoreSubFolders_Task(datastorePath=ds_path, searchSpec=spec)
            results = wait_task(task) or []
            count = 0
            for r in results:
                if getattr(r, "file", None):
                    count += len(r.file)
        else:
            task = browser.SearchDatastore_Task(datastorePath=ds_path, searchSpec=spec)
            result = wait_task(task)
            count = len(result.file) if result and getattr(result, "file", None) else 0
    except Exception as e:
        print(f"UNKNOWN - datastore browse failed: {e}")
        sys.exit(3)

    # Evaluate thresholds
    state = "OK"
    rc = 0
    if count > args.critical:
        state, rc = "CRITICAL", 2
    elif args.warning and count >= args.warning:
        state, rc = "WARNING", 1

    mode = "snaponly" if args.snaponly else "allfiles"
    scope = "recursive" if args.recursive else "folder"
    print(f"{state} - files={count} vm='{vm.name}' ds='{ds_name}' path='{ds_path}' mode={mode} scope={scope} | files={count};{args.warning};{args.critical};0;")
    sys.exit(rc)

if __name__ == "__main__":
    main()
