//! 解码缓冲的有界共享内存 arena。
//!
//! Arena 是解码后字节唯一存放的地方。调用方拿到一个不透明的句柄加上
//! offset/length 对；arena 拒绝超出容量无限增长。字节不会被拷贝进控制消息或日志。

use crate::shm::ShmSegment;
use crate::MediaError;

pub const DEFAULT_ARENA_CAPACITY_BYTES: usize = 64 * 1024 * 1024;

/// 字节的实际存放位置：本进程内（默认）或 POSIX 共享内存（跨进程交接）。
#[derive(Debug)]
enum Backing {
    Anonymous(Vec<u8>),
    Shared(ShmSegment),
}

impl Backing {
    fn as_slice(&self) -> &[u8] {
        match self {
            Self::Anonymous(bytes) => bytes,
            Self::Shared(segment) => segment.as_slice(),
        }
    }

    fn as_mut_slice(&mut self) -> &mut [u8] {
        match self {
            Self::Anonymous(bytes) => bytes,
            Self::Shared(segment) => segment.as_mut_slice(),
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Slab {
    offset: usize,
    length: usize,
    live: bool,
}

#[derive(Debug)]
pub struct Arena {
    id: String,
    capacity: usize,
    bytes: Backing,
    /// 段名只在 lease 服务里交给消费者，绝不进入 descriptor。
    segment_name: Option<String>,
    slabs: Vec<Slab>,
    used_bytes: usize,
    peak_bytes: usize,
}

impl Arena {
    pub fn new(id: &str, capacity: usize) -> Result<Self, MediaError> {
        if id.is_empty() || capacity == 0 {
            return Err(MediaError::IoFailed("invalid_arena_configuration".into()));
        }
        Ok(Self {
            id: id.to_string(),
            capacity,
            bytes: Backing::Anonymous(vec![0; capacity]),
            segment_name: None,
            slabs: Vec::new(),
            used_bytes: 0,
            peak_bytes: 0,
        })
    }

    /// 创建一个可跨进程读取的 arena。`seed` 决定段名，因此段名不可从 `id` 推导。
    pub fn shared(id: &str, capacity: usize, seed: &[u8]) -> Result<Self, MediaError> {
        if id.is_empty() || capacity == 0 {
            return Err(MediaError::IoFailed("invalid_arena_configuration".into()));
        }
        let (segment, name) = ShmSegment::create(seed, capacity)?;
        Ok(Self {
            id: id.to_string(),
            capacity,
            bytes: Backing::Shared(segment),
            segment_name: Some(name),
            slabs: Vec::new(),
            used_bytes: 0,
            peak_bytes: 0,
        })
    }

    /// 交给 data-plane 消费者使用的不透明句柄；绝不是宿主指针。
    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// 跨进程消费者用来建立映射的段名。匿名 arena 返回 `None`。
    pub fn segment_name(&self) -> Option<&str> {
        self.segment_name.as_deref()
    }

    pub fn is_shared(&self) -> bool {
        self.segment_name.is_some()
    }

    pub fn used_bytes(&self) -> usize {
        self.used_bytes
    }

    pub fn peak_bytes(&self) -> usize {
        self.peak_bytes
    }

    pub fn live_slabs(&self) -> usize {
        self.slabs.iter().filter(|slab| slab.live).count()
    }

    /// 分配一个 `length` 字节的 slab 并返回其 offset。
    pub fn allocate(&mut self, length: usize) -> Result<usize, MediaError> {
        if length == 0 {
            return Err(MediaError::IoFailed("zero_length_allocation".into()));
        }
        if let Some(offset) = self.reuse(length)? {
            return Ok(offset);
        }
        if self.used_bytes + length > self.capacity {
            return Err(MediaError::ArenaCapacityExceeded {
                requested: length,
                capacity: self.capacity,
            });
        }
        let offset = self.used_bytes;
        self.used_bytes += length;
        self.peak_bytes = self.peak_bytes.max(self.used_bytes);
        self.slabs.push(Slab {
            offset,
            length,
            live: true,
        });
        self.slabs.sort_by_key(|slab| slab.offset);
        Ok(offset)
    }

    pub fn write(&mut self, offset: usize, payload: &[u8]) -> Result<(), MediaError> {
        self.slab(offset, payload.len())?;
        self.bytes.as_mut_slice()[offset..offset + payload.len()].copy_from_slice(payload);
        Ok(())
    }

    /// 读回一个 live slab 内部的任意连续区间。交接给消费者的是"读取窗口"，
    /// 窗口可以比 slab 窄、也可以从 slab 中间开始；越界或跨段一律拒绝。
    pub fn read(&self, offset: usize, length: usize) -> Result<&[u8], MediaError> {
        self.containing_slab(offset, length)?;
        Ok(&self.bytes.as_slice()[offset..offset + length])
    }

    /// 释放一个 slab 并与相邻空闲空间合并。
    pub fn release(&mut self, offset: usize) -> bool {
        let Some(index) = self.slabs.iter().position(|slab| slab.offset == offset) else {
            return false;
        };
        if !self.slabs[index].live {
            return false;
        }
        self.slabs[index].live = false;
        self.merge();
        true
    }

    fn reuse(&mut self, length: usize) -> Result<Option<usize>, MediaError> {
        let Some(index) = self
            .slabs
            .iter()
            .position(|slab| !slab.live && slab.length >= length)
        else {
            return Ok(None);
        };
        let slab = self.slabs[index];
        if slab.length == length {
            self.slabs[index].live = true;
            return Ok(Some(slab.offset));
        }
        self.slabs[index] = Slab {
            offset: slab.offset,
            length,
            live: true,
        };
        self.slabs.insert(
            index + 1,
            Slab {
                offset: slab.offset + length,
                length: slab.length - length,
                live: false,
            },
        );
        self.slabs.sort_by_key(|candidate| candidate.offset);
        Ok(Some(slab.offset))
    }

    fn merge(&mut self) {
        self.slabs.sort_by_key(|slab| slab.offset);
        let mut merged: Vec<Slab> = Vec::with_capacity(self.slabs.len());
        for slab in self.slabs.drain(..) {
            match merged.last_mut() {
                Some(previous)
                    if !previous.live
                        && !slab.live
                        && previous.offset + previous.length == slab.offset =>
                {
                    previous.length += slab.length;
                }
                _ => merged.push(slab),
            }
        }
        self.slabs = merged;
    }

    /// 找到完全包含 `[offset, offset + length)` 的 live slab。
    fn containing_slab(&self, offset: usize, length: usize) -> Result<Slab, MediaError> {
        if length == 0 {
            return Err(MediaError::IoFailed("zero_length_slab".into()));
        }
        let end = offset
            .checked_add(length)
            .ok_or_else(|| MediaError::IoFailed("unknown_or_released_arena_slab".into()))?;
        self.slabs
            .iter()
            .find(|slab| slab.live && slab.offset <= offset && slab.offset + slab.length >= end)
            .copied()
            .ok_or_else(|| MediaError::IoFailed("unknown_or_released_arena_slab".into()))
    }

    fn slab(&self, offset: usize, length: usize) -> Result<Slab, MediaError> {
        if length == 0 {
            return Err(MediaError::IoFailed("zero_length_slab".into()));
        }
        self.slabs
            .iter()
            .find(|slab| slab.live && slab.offset == offset && slab.length >= length)
            .copied()
            .ok_or_else(|| MediaError::IoFailed("unknown_or_released_arena_slab".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_bytes_and_tracks_peak_usage() {
        let mut arena = Arena::new("arena-test", 64).unwrap();
        let offset = arena.allocate(16).unwrap();
        arena.write(offset, &[7u8; 16]).unwrap();
        assert_eq!(arena.read(offset, 16).unwrap(), &[7u8; 16]);
        assert_eq!(arena.used_bytes(), 16);
        assert_eq!(arena.peak_bytes(), 16);
        assert_eq!(arena.id(), "arena-test");
    }

    #[test]
    fn refuses_to_grow_past_capacity() {
        let mut arena = Arena::new("arena-test", 32).unwrap();
        arena.allocate(24).unwrap();
        assert!(matches!(
            arena.allocate(16),
            Err(MediaError::ArenaCapacityExceeded { .. })
        ));
        assert!(Arena::new("arena-test", 0).is_err());
        assert!(arena.allocate(0).is_err());
    }

    #[test]
    fn release_allows_reuse_and_merges_neighbours() {
        let mut arena = Arena::new("arena-test", 32).unwrap();
        let first = arena.allocate(8).unwrap();
        let second = arena.allocate(8).unwrap();
        assert_eq!((first, second), (0, 8));
        assert!(arena.release(first));
        assert!(!arena.release(first));
        assert!(arena.release(second));
        assert_eq!(
            arena.used_bytes(),
            16,
            "released slabs keep their reserved size"
        );
        assert_eq!(arena.live_slabs(), 0);
        // 合并后的空闲区域会被复用，而不是再追加新空间。
        let reused = arena.allocate(16).unwrap();
        assert_eq!(reused, 0);
        assert_eq!(arena.used_bytes(), 16);
    }

    #[test]
    fn split_free_space_is_usable_again() {
        let mut arena = Arena::new("arena-test", 32).unwrap();
        let big = arena.allocate(24).unwrap();
        arena.release(big);
        let small = arena.allocate(8).unwrap();
        let other = arena.allocate(8).unwrap();
        assert_eq!((small, other), (0, 8));
        assert_eq!(arena.used_bytes(), 24);
        assert_eq!(arena.read(small, 8).unwrap().len(), 8);
    }

    #[test]
    fn reading_released_or_unknown_slabs_fails() {
        let mut arena = Arena::new("arena-test", 32).unwrap();
        let offset = arena.allocate(8).unwrap();
        assert!(arena.read(offset, 8).is_ok());
        arena.release(offset);
        assert!(arena.read(offset, 8).is_err());
        assert!(arena.read(31, 8).is_err());
        assert!(arena.write(0, &[]).is_err());
    }

    #[test]
    fn a_shared_arena_is_readable_through_an_independent_mapping() {
        let mut arena = Arena::shared("arena-shared", 4096, b"shared-arena-test").unwrap();
        let name = arena
            .segment_name()
            .expect("shared arenas publish a name")
            .to_string();
        assert!(arena.is_shared());
        let offset = arena.allocate(5).unwrap();
        arena.write(offset, b"hello").unwrap();
        // 第二个映射代表另一个进程：它必须看到同样的字节，且看不到未分配的尾部。
        let reader = ShmSegment::open(&name, 4096).unwrap();
        assert_eq!(&reader.as_slice()[offset..offset + 5], b"hello");
        assert_eq!(&reader.as_slice()[offset + 5..offset + 6], &[0u8]);
        drop(reader);
        assert!(Arena::new("arena-anon", 4096)
            .unwrap()
            .segment_name()
            .is_none());
    }
}
