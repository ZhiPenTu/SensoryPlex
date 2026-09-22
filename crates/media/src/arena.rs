//! Bounded shared-memory arena for decoded buffers.
//!
//! The arena is the only place decoded bytes live. Callers receive an opaque handle plus an
//! offset/length pair, and the arena refuses to exceed its capacity instead of growing without
//! limit. Bytes are never copied into control messages or logs.

use crate::MediaError;

pub const DEFAULT_ARENA_CAPACITY_BYTES: usize = 64 * 1024 * 1024;

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
    bytes: Vec<u8>,
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
            bytes: vec![0; capacity],
            slabs: Vec::new(),
            used_bytes: 0,
            peak_bytes: 0,
        })
    }

    /// Opaque handle handed to data-plane consumers. Never a host pointer.
    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn capacity(&self) -> usize {
        self.capacity
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

    /// Allocates a slab of `length` bytes and returns its offset.
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
        self.bytes[offset..offset + payload.len()].copy_from_slice(payload);
        Ok(())
    }

    /// Reads back a live slab. Used to verify a lease handoff without copying bytes elsewhere.
    pub fn read(&self, offset: usize, length: usize) -> Result<&[u8], MediaError> {
        self.slab(offset, length)?;
        Ok(&self.bytes[offset..offset + length])
    }

    /// Frees a slab and merges it with neighbouring free space.
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
        // The merged free region is reused instead of appending new space.
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
}
