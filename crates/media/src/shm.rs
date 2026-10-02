//! POSIX 共享内存段：数据面跨进程读写的载体。
//!
//! descriptor 里的句柄始终是运行时不透明 handle，段名只在 lease 服务的应答里出现，
//! 且每次运行随机生成——知道 handle 不等于能读字节（见 ADR-010）。

use std::ffi::CString;
use std::os::fd::RawFd;

use sha2::{Digest, Sha256};

use crate::MediaError;

/// 每次运行一次的随机种子来源。只用系统时钟、pid 与 ASLR 地址，不引入新的依赖；
/// 它必须"不可从 arena/handle 推导"，而不是密码学级的随机源。
pub fn run_seed() -> Vec<u8> {
    let stack_marker = 0u8;
    let mut seed = Vec::with_capacity(48);
    seed.extend_from_slice(
        &std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or_default()
            .to_le_bytes(),
    );
    seed.extend_from_slice(&u64::from(std::process::id()).to_le_bytes());
    seed.extend_from_slice(&(std::ptr::addr_of!(stack_marker) as usize as u64).to_le_bytes());
    seed
}

/// 由运行时随机派生段名（不可从 handle 推导）。
pub fn derive_segment_name(seed: &[u8]) -> String {
    let digest = Sha256::digest(seed);
    let tag: String = digest[..6]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    format!("/sp.{tag}")
}

/// 一个已建立映射的共享内存段。`Drop` 时解除映射、关闭 fd、删除段名。
#[derive(Debug)]
pub struct ShmSegment {
    ptr: *mut u8,
    len: usize,
    fd: RawFd,
    name: CString,
    owner: bool,
    fallback_path: Option<CString>,
}

// 映射本身是进程私有的地址空间，访问由持有者串行化；段名与 fd 都是进程局部资源。
unsafe impl Send for ShmSegment {}

impl ShmSegment {
    /// 创建（或按需重建）一个段并映射。
    pub fn create(seed: &[u8], capacity: usize) -> Result<(Self, String), MediaError> {
        if capacity == 0 {
            return Err(MediaError::IoFailed("zero_length_shared_arena".into()));
        }
        let name = derive_segment_name(seed);
        let segment = Self::open_named(&name, capacity, true)?;
        Ok((segment, name))
    }

    /// 打开一个已存在的段（消费者侧）。容量必须显式给出：段长不是可以猜的东西。
    pub fn open(name: &str, capacity: usize) -> Result<Self, MediaError> {
        Self::open_named(name, capacity, false)
    }

    fn open_named(name: &str, capacity: usize, owner: bool) -> Result<Self, MediaError> {
        let c_name =
            CString::new(name).map_err(|_| MediaError::IoFailed("invalid_segment_name".into()))?;
        let flags = if owner {
            libc::O_CREAT | libc::O_RDWR
        } else {
            libc::O_RDWR
        };
        let mut fallback_path = None;
        let mut fd = unsafe { libc::shm_open(c_name.as_ptr(), flags, 0o600) };
        if fd < 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EPERM)
                || err.raw_os_error() == Some(libc::EACCES)
                || (!owner && err.raw_os_error() == Some(libc::ENOENT))
            {
                let clean = name.trim_start_matches('/');
                let fallback = format!("/tmp/sensoryplex-shm-{clean}");
                if let Ok(c_fallback) = CString::new(fallback) {
                    let file_fd = unsafe { libc::open(c_fallback.as_ptr(), flags, 0o600) };
                    if file_fd >= 0 {
                        fd = file_fd;
                        fallback_path = Some(c_fallback);
                    }
                }
            }
        }
        if fd < 0 {
            return Err(MediaError::IoFailed(format!(
                "shm_open_failed: {}",
                std::io::Error::last_os_error()
            )));
        }
        if owner && unsafe { libc::ftruncate(fd, capacity as libc::off_t) } != 0 {
            let error = std::io::Error::last_os_error();
            unsafe { libc::close(fd) };
            if let Some(ref path) = fallback_path {
                unsafe { libc::unlink(path.as_ptr()) };
            } else {
                unsafe { libc::shm_unlink(c_name.as_ptr()) };
            }
            return Err(MediaError::IoFailed(format!(
                "shm_ftruncate_failed: {error}"
            )));
        }
        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                capacity,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd,
                0,
            )
        };
        if ptr == libc::MAP_FAILED {
            let error = std::io::Error::last_os_error();
            unsafe { libc::close(fd) };
            if owner {
                if let Some(ref path) = fallback_path {
                    unsafe { libc::unlink(path.as_ptr()) };
                } else {
                    unsafe { libc::shm_unlink(c_name.as_ptr()) };
                }
            }
            return Err(MediaError::IoFailed(format!("shm_mmap_failed: {error}")));
        }
        Ok(Self {
            ptr: ptr.cast::<u8>(),
            len: capacity,
            fd,
            name: c_name,
            owner,
            fallback_path,
        })
    }

    pub fn as_slice(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
    }

    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.len) }
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

impl Drop for ShmSegment {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.ptr.cast(), self.len);
            libc::close(self.fd);
            // 只有创建者负责删名；消费者关闭自己的映射即可。删除段名不影响已经映射的消费者。
            if self.owner {
                if let Some(ref path) = self.fallback_path {
                    libc::unlink(path.as_ptr());
                } else {
                    libc::shm_unlink(self.name.as_ptr());
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn segment_names_are_derived_from_the_seed_and_bounded() {
        let a = derive_segment_name(b"arena-0123456789ab:1");
        let b = derive_segment_name(b"arena-0123456789ab:2");
        assert_ne!(a, b);
        assert_eq!(a, derive_segment_name(b"arena-0123456789ab:1"));
        assert!(a.starts_with("/sp."));
        // macOS 的 PSHMNAMLEN 是 31，段名必须留出余量。
        assert!(a.len() < 24, "{a}");
    }

    #[test]
    fn two_independent_mappings_see_the_same_bytes() {
        let (mut writer, name) = ShmSegment::create(b"test-second-mapping", 4096).unwrap();
        writer.as_mut_slice()[..5].copy_from_slice(b"hello");
        // 第二个映射模拟另一个进程：它看到的一定是同一份字节。
        let reader = ShmSegment::open(&name, 4096).unwrap();
        assert_eq!(&reader.as_slice()[..5], b"hello");
        assert_eq!(reader.len(), 4096);
        drop(reader);
        assert_eq!(&writer.as_slice()[..5], b"hello");
    }

    #[test]
    fn segment_names_differ_between_runs() {
        // 同一个 arena 在两次运行里得到不同段名：知道 handle 推不出段名。
        let first = derive_segment_name(&run_seed());
        let second = derive_segment_name(&run_seed());
        assert_ne!(first, second);
    }

    #[test]
    fn opening_an_unknown_segment_fails_instead_of_creating_one() {
        assert!(ShmSegment::open("/sp.does-not-exist", 4096).is_err());
        assert!(ShmSegment::create(b"", 0).is_err());
    }
}
