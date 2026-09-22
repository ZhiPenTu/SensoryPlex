//! 由运行时签发的、只读的 buffer lease。

use sensoryplex_sdk::{common::BufferLease, ContractError};

/// 存续中的 lease 数量是有界的；data plane 无界是缺陷，不是限流策略。
pub const MAX_OUTSTANDING_LEASES: usize = 4096;

#[derive(Debug, Default)]
pub struct LeaseRegistry {
    issued: u64,
    outstanding: Vec<BufferLease>,
}

impl LeaseRegistry {
    /// 签发一个只读 lease。`ttl_ms` 为 0 时直接拒绝，而不是默认取值。
    pub fn issue(&mut self, now_ms: i64, ttl_ms: u32) -> Result<BufferLease, ContractError> {
        if ttl_ms == 0 {
            return Err(ContractError("invalid_lease_ttl"));
        }
        self.expire(now_ms);
        if self.outstanding.len() >= MAX_OUTSTANDING_LEASES {
            return Err(ContractError("lease_capacity_exhausted"));
        }
        self.issued += 1;
        let lease = BufferLease {
            lease_id: format!("lease-{:016x}", self.issued),
            expires_at_unix_ms: now_ms.saturating_add(i64::from(ttl_ms)),
            read_only: true,
        };
        self.outstanding.push(lease.clone());
        Ok(lease)
    }

    /// 仅接受本 registry 签发、未过期且仍为只读的 lease。
    pub fn validate(&self, lease: &BufferLease, now_ms: i64) -> Result<(), ContractError> {
        let held = self
            .outstanding
            .iter()
            .find(|candidate| candidate.lease_id == lease.lease_id)
            .ok_or(ContractError("unknown_or_released_lease"))?;
        if held.expires_at_unix_ms != lease.expires_at_unix_ms {
            return Err(ContractError("lease_expiry_mismatch"));
        }
        if !lease.read_only {
            return Err(ContractError("lease_is_read_only"));
        }
        if held.expires_at_unix_ms <= now_ms {
            return Err(ContractError("invalid_or_expired_lease"));
        }
        Ok(())
    }

    /// 提前释放 lease。lease 不存在或已被释放时返回 false。
    pub fn release(&mut self, lease_id: &str) -> bool {
        let before = self.outstanding.len();
        self.outstanding.retain(|lease| lease.lease_id != lease_id);
        self.outstanding.len() != before
    }

    /// 丢弃过期 lease 并返回被回收的数量。
    pub fn expire(&mut self, now_ms: i64) -> usize {
        let before = self.outstanding.len();
        self.outstanding
            .retain(|lease| lease.expires_at_unix_ms > now_ms);
        before - self.outstanding.len()
    }

    pub fn outstanding(&self) -> usize {
        self.outstanding.len()
    }

    /// 仍在册的 lease id。用于把"buffer 被谁占着"与"lease 是否还活着"对上。
    pub fn outstanding_ids(&self) -> Vec<String> {
        self.outstanding
            .iter()
            .map(|lease| lease.lease_id.clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_zero_ttl_and_expired_leases() {
        let mut registry = LeaseRegistry::default();
        assert!(registry.issue(1_000, 0).is_err());
        let lease = registry.issue(1_000, 500).unwrap();
        assert!(lease.read_only);
        assert!(registry.validate(&lease, 1_499).is_ok());
        assert!(registry.validate(&lease, 1_500).is_err());
        assert_eq!(registry.outstanding(), 1);
        assert_eq!(registry.expire(1_500), 1);
        assert_eq!(registry.outstanding(), 0);
    }

    #[test]
    fn rejects_extended_or_writable_forgeries() {
        let mut registry = LeaseRegistry::default();
        let lease = registry.issue(0, 1_000).unwrap();
        let mut extended = lease.clone();
        extended.expires_at_unix_ms += 60_000;
        assert!(registry.validate(&extended, 1).is_err());
        let mut writable = lease.clone();
        writable.read_only = false;
        assert!(registry.validate(&writable, 1).is_err());
        let mut unknown = lease.clone();
        unknown.lease_id = "lease-ffffffffffffffff".into();
        assert!(registry.validate(&unknown, 1).is_err());
    }

    #[test]
    fn early_release_and_capacity_are_bounded() {
        let mut registry = LeaseRegistry::default();
        let lease = registry.issue(0, 10_000).unwrap();
        assert!(registry.release(&lease.lease_id));
        assert!(!registry.release(&lease.lease_id));
        assert!(registry.validate(&lease, 1).is_err());
        for _ in 0..MAX_OUTSTANDING_LEASES {
            registry.issue(0, 10_000).unwrap();
        }
        assert_eq!(registry.outstanding(), MAX_OUTSTANDING_LEASES);
        assert!(registry.issue(0, 10_000).is_err());
    }
}
