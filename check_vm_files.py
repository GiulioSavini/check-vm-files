#!/usr/bin/env python3
import ssl
import sys
import time
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
    """Parse '[datastore1] folder/vm.vmx' -> ('datastore1', 'folder')"""
    if not vm_path_name or not vm_path_name.startswith("[") or "]" not in vm_path_name:
        raise ValueError(f"Unexpected vmPathName format: {vm_path_name!r}")
    ds = vm_path_name[1:vm_path_name.index("]")]
    rest = vm_path_name[vm_path_name.index("]")+1:].strip()
    if "/" not in rest:
        folder = ""
    else:
        folder = rest.rsplit("/", 1)[0]
    return ds, folder

def wait_task(task):
    while True:
        state = task.info.state
        if state == vim.TaskInfo.State.success:
            return task.info.result
        if state == vim.TaskInfo.State.error:
            raise task.info.error
        time.sleep(0.2)

def browse_datastore(browser, ds_name, folder, spec, recursive):
    """Browse a single datastore for matching files. Returns count."""
    if folder:
        ds_path = f"[{ds_name}] {folder}"
    else:
        ds_path = f"[{ds_name}]"

    try:
        if recursive:
            task = browser.SearchDatastoreSubFolders_Task(datastorePath=ds_path, searchSpec=spec)
            results = wait_task(task) or []
            count = 0
            for r in results:
                if getattr(r, "file", None):
                    count += len(r.file)
            return count
        else:
            task = browser.SearchDatastore_Task(datastorePath=ds_path, searchSpec=spec)
            result = wait_task(task)
            return len(result.file) if result and getattr(result, "file", None) else 0
    except vim.fault.FileNotFound:
        # VM folder does not exist on this datastore - normal for multi-ds VMs
        return 0
    except Exception:
        # Folder not present on this datastore, skip silently
        return 0

def get_vm_folder_name(vm):
    """Get the VM folder name from layout files or vmPathName."""
    # Try to collect all unique datastore paths from the VM layout
    folders = set()

    # Primary: from vmPathName
    if vm.config and vm.config.files and vm.config.files.vmPathName:
        try:
            _, folder = parse_vm_path(vm.config.files.vmPathName)
            if folder:
                folders.add(folder)
        except ValueError:
            pass

    # Additional: from layout.file (lists all files across all datastores)
    if hasattr(vm, 'layoutEx') and vm.layoutEx and vm.layoutEx.file:
        for f in vm.layoutEx.file:
            try:
                _, folder = parse_vm_path(f.name)
                if folder:
                    folders.add(folder)
            except (ValueError, AttributeError):
                pass

    return folders

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

    # Get all folder names used by the VM across datastores
    vm_folders = get_vm_folder_name(vm)
    if not vm_folders:
        # Fallback: use VM name as folder name (common convention)
        vm_folders = {args.vm}

    # Get all datastores attached to the VM
    datastores = vm.datastore
    if not datastores:
        print(f"UNKNOWN - VM '{args.vm}' has no datastores")
        sys.exit(3)

    # Build search spec
    spec = vim.HostDatastoreBrowserSearchSpec()
    if args.snaponly:
        spec.matchPattern = ["*-0000*.vmdk", "*.vmsn", "*.vmsd", "*.delta.vmdk"]
    else:
        spec.matchPattern = ["*.vmdk"]

    # Browse ALL datastores for VM files
    total_count = 0
    ds_details = []
    errors = []

    for ds in datastores:
        ds_name = ds.summary.name if ds.summary else "unknown"
        try:
            browser = ds.browser
        except Exception as e:
            errors.append(f"{ds_name}: no browser ({e})")
            continue

        ds_count = 0
        for folder in vm_folders:
            ds_count += browse_datastore(browser, ds_name, folder, spec, args.recursive)

        if ds_count > 0:
            ds_details.append(f"{ds_name}={ds_count}")
        total_count += ds_count

    # Evaluate thresholds
    state = "OK"
    rc = 0
    if total_count > args.critical:
        state, rc = "CRITICAL", 2
    elif args.warning and total_count >= args.warning:
        state, rc = "WARNING", 1

    mode = "snaponly" if args.snaponly else "allfiles"
    scope = "recursive" if args.recursive else "folder"
    ds_info = ", ".join(ds_details) if ds_details else "none"
    ds_count_total = len(datastores)

    msg = (f"{state} - files={total_count} vm='{vm.name}' "
           f"datastores={ds_count_total} [{ds_info}] "
           f"mode={mode} scope={scope}")
    if errors:
        msg += f" errors=[{', '.join(errors)}]"
    msg += f" | files={total_count};{args.warning};{args.critical};0;"

    print(msg)
    sys.exit(rc)

if __name__ == "__main__":
    main()
