use test_infra::*;

const CLEAR_LINUX_KERNEL_CMDLINE: &str = "mitigations=off quiet console=hvc0 console=tty0 console=ttyS0,115200n8 cryptomgr.notests init=/usr/lib/systemd/systemd-bootchart initcall_debug no_timer_check noreplace-smp page_alloc.shuffle=1 rootfstype=ext4,btrfs,xfs,f2fs root=/dev/vda2 tsc=reliable rw";

#[test]
fn test_clear_linux_guest() {
    let mut workload_path = dirs::home_dir().unwrap();
    workload_path.push("workloads");
    let mut kernel_path = workload_path.clone();
    kernel_path.push("vmlinux");
    let guest = Guest::new(Box::new(ClearDiskConfig::new(
        "clear-37720-cloudguest.img".to_string(),
    )));
    let mut child = GuestCommand::new(&guest)
        .args(["--cpus", "boot=2,affinity=[0@[0,1],1@[1]]"])
        .args(["--memory", "size=1G"])
        .args(["--kernel", kernel_path.to_str().unwrap()])
        .args(["--cmdline", CLEAR_LINUX_KERNEL_CMDLINE])
        .default_disks()
        .default_net()
        .capture_output()
        .spawn()
        .unwrap();

    let r = std::panic::catch_unwind(|| {
        guest.wait_vm_boot(None).unwrap();
        assert_eq!(guest.get_cpu_count().unwrap_or_default(), 2);
    });

    let _ = child.kill();
    let output = child.wait_with_output().unwrap();

    handle_child_output(r, &output);
}
