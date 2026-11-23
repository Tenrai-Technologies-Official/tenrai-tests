//! Which process dialed a loopback TCP connection, and a 1 ms timer where the OS needs asking.

#[cfg(windows)]
mod imp {
    #[link(name = "iphlpapi")]
    unsafe extern "system" {
        fn GetExtendedTcpTable(
            table: *mut u8,
            size: *mut u32,
            order: i32,
            af: u32,
            class: i32,
            reserved: u32,
        ) -> u32;
    }

    #[link(name = "winmm")]
    unsafe extern "system" {
        fn timeBeginPeriod(period: u32) -> u32;
    }

    /// Windows wakes timers every 15.6 ms unless a process asks for less.
    pub fn fine_timer() {
        unsafe { timeBeginPeriod(1) };
    }

    fn port_of(raw: u32) -> u16 {
        (((raw & 0xff) << 8) | ((raw >> 8) & 0xff)) as u16
    }

    /// The process whose socket at 127.0.0.1:local is connected to 127.0.0.1:remote.
    pub fn owner_of(local: u16, remote: u16) -> Option<u32> {
        const AF_INET: u32 = 2;
        const TCP_TABLE_OWNER_PID_ALL: i32 = 5;
        let mut size = 0_u32;
        unsafe {
            GetExtendedTcpTable(
                std::ptr::null_mut(),
                &mut size,
                0,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
        };
        let mut table = vec![0_u8; size as usize + 64 * 1024];
        let mut size = table.len() as u32;
        let status = unsafe {
            GetExtendedTcpTable(
                table.as_mut_ptr(),
                &mut size,
                0,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
        };
        if status != 0 {
            return None;
        }
        let word = |at: usize| u32::from_ne_bytes(table[at..at + 4].try_into().unwrap());
        let count = word(0) as usize;
        (0..count).find_map(|row| {
            let at = 4 + row * 24;
            (port_of(word(at + 8)) == local && port_of(word(at + 16)) == remote)
                .then(|| word(at + 20))
        })
    }
}

#[cfg(not(windows))]
mod imp {
    pub fn fine_timer() {}

    /// lsof lists both ends of the loopback connection; the dialer owns `local->remote`.
    pub fn owner_of(local: u16, remote: u16) -> Option<u32> {
        let output = std::process::Command::new("lsof")
            .args(["-nP", &format!("-iTCP@127.0.0.1:{local}"), "-Fpn"])
            .output()
            .ok()?;
        let wanted = format!("n127.0.0.1:{local}->127.0.0.1:{remote}");
        let mut pid = None;
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            if let Some(value) = line.strip_prefix('p') {
                pid = value.parse().ok();
            } else if line == wanted {
                return pid;
            }
        }
        None
    }
}

pub use imp::{fine_timer, owner_of};
