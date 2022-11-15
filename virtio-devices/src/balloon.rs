// Copyright (c) 2020 Ant Financial
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use crate::{
    seccomp_filters::Thread, thread_helper::spawn_virtio_thread, ActivateResult, EpollHelper,
    EpollHelperError, EpollHelperHandler, GuestMemoryMmap, VirtioCommon, VirtioDevice,
    VirtioDeviceType, VirtioInterrupt, VirtioInterruptType, EPOLL_HELPER_EVENT_LAST,
    VIRTIO_F_VERSION_1,
};
use anyhow::anyhow;
use seccompiler::SeccompAction;
use std::collections::HashMap;
use std::io::{self, Write};
use std::mem::size_of;
use std::num::Wrapping;
use std::os::unix::io::AsRawFd;
use std::result;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{atomic::AtomicBool, Arc, Barrier};
use thiserror::Error;
use versionize::{VersionMap, Versionize, VersionizeResult};
use versionize_derive::Versionize;
use virtio_queue::{Queue, QueueT};
use vm_allocator::page_size::{align_page_size_down, get_page_size};
use vm_memory::{
    Address, ByteValued, Bytes, GuestAddress, GuestAddressSpace, GuestMemory, GuestMemoryAtomic,
    GuestMemoryError, GuestMemoryRegion,
};
use vm_migration::{
    Migratable, MigratableError, Pausable, Snapshot, Snapshottable, Transportable, VersionMapped,
};
use vmm_sys_util::eventfd::EventFd;

const QUEUE_SIZE: u16 = 128;
const REPORTING_QUEUE_SIZE: u16 = 32;
const MIN_NUM_QUEUES: usize = 2;

// Inflate virtio queue event.
const INFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 1;
// Deflate virtio queue event.
const DEFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 2;
// Memory statistics virtio queue event.
const STATS_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 3;
// Reporting virtio queue event.
const REPORTING_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 4;
// Heterogeneous inflate virtio queue event.
const HETERO_INFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 5;
// Heterogeneous deflate virtio queue event.
const HETERO_DEFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 6;

// Size of a PFN in the balloon interface.
const VIRTIO_BALLOON_PFN_SHIFT: u64 = 12;

// Memory statistics virtqueue
const VIRTIO_BALLOON_F_STATS_VQ: u64 = 1;
// Deflate balloon on OOM
const VIRTIO_BALLOON_F_DEFLATE_ON_OOM: u64 = 2;
// Enable an additional virtqueue to let the guest notify the host about free
// pages.
const VIRTIO_BALLOON_F_REPORTING: u64 = 5;
// Enable an additional pair of inflate and deflate virtqueues to handle ballooning of heterogeneous memory
const VIRTIO_BALLOON_F_HETERO_MEM: u64 = 6;

#[derive(Error, Debug)]
pub enum Error {
    #[error("Guest gave us bad memory addresses.: {0}")]
    GuestMemory(GuestMemoryError),
    #[error("Guest gave us a write only descriptor that protocol says to read from")]
    UnexpectedWriteOnlyDescriptor,
    #[error("Guest sent us invalid request")]
    InvalidRequest,
    #[error("Fallocate fail.: {0}")]
    FallocateFail(std::io::Error),
    #[error("Madvise fail.: {0}")]
    MadviseFail(std::io::Error),
    #[error("Failed to EventFd write.: {0}")]
    EventFdWriteFail(std::io::Error),
    #[error("Invalid queue index: {0}")]
    InvalidQueueIndex(usize),
    #[error("Fail tp signal: {0}")]
    FailedSignal(io::Error),
    #[error("Descriptor chain is too short")]
    DescriptorChainTooShort,
    #[error("Failed adding used index: {0}")]
    QueueAddUsed(virtio_queue::Error),
    #[error("Failed creating an iterator over the queue: {0}")]
    QueueIterator(virtio_queue::Error),
    #[error("Guest sent an unexpected balloon statistic tag: {0}")]
    UnexpectedStatTag(u16),
}

// Got from include/uapi/linux/virtio_balloon.h
#[repr(C)]
#[derive(Copy, Clone, Debug, Default, Versionize)]
pub struct VirtioBalloonConfig {
    // Number of pages host wants Guest to give up.
    num_pages: u32,
    // Number of pages we've actually got in balloon.
    actual: u32,
    // Free page hinting to speed up migration (this feature is not implemented).
    // Caveat: should not be mixed with free page reporting
    hint_cmd_id: u32,
    // Deflated or reported free pages are initialized with this value (this feature is not implemented).
    poison_val: u32,
    // Number of heterogeneous pages host wants Guest to give up.
    num_hetero_pages: u32,
    // Number of heterogeneous pages we've actually got in balloon.
    hetero_actual: u32,
}

#[derive(Clone, Debug)]
struct PartiallyBalloonedPage {
    addr: u64,
    bitmap: Vec<u64>,
    page_size: u64,
}

impl PartiallyBalloonedPage {
    fn new() -> Self {
        let page_size = get_page_size();
        let len = ((page_size >> VIRTIO_BALLOON_PFN_SHIFT) + 63) / 64;
        // Initial each padding bit as 1 in bitmap.
        let mut bitmap = vec![0_u64; len as usize];
        let pad_num = len * 64 - (page_size >> VIRTIO_BALLOON_PFN_SHIFT);
        bitmap[(len - 1) as usize] = !((1 << (64 - pad_num)) - 1);
        Self {
            addr: 0,
            bitmap,
            page_size,
        }
    }

    fn pfn_match(&self, addr: u64) -> bool {
        self.addr == addr & !(self.page_size - 1)
    }

    fn bitmap_full(&self) -> bool {
        self.bitmap.iter().all(|b| *b == u64::MAX)
    }

    fn set_bit(&mut self, addr: u64) {
        let addr_offset = (addr % self.page_size) >> VIRTIO_BALLOON_PFN_SHIFT;
        self.bitmap[(addr_offset / 64) as usize] |= 1 << (addr_offset % 64);
    }

    fn reset(&mut self) {
        let len = ((self.page_size >> VIRTIO_BALLOON_PFN_SHIFT) + 63) / 64;
        self.addr = 0;
        self.bitmap = vec![0; len as usize];
        let pad_num = len * 64 - (self.page_size >> VIRTIO_BALLOON_PFN_SHIFT);
        self.bitmap[(len - 1) as usize] = !((1 << (64 - pad_num)) - 1);
    }
}

const CONFIG_ACTUAL_OFFSET: u64 = 4;
const CONFIG_HETERO_ACTUAL_OFFSET: u64 = 20;
const CONFIG_ACTUAL_SIZE: usize = 4;

// SAFETY: it only has data and has no implicit padding.
unsafe impl ByteValued for VirtioBalloonConfig {}

struct BalloonEpollHandler {
    mem: GuestMemoryAtomic<GuestMemoryMmap>,
    queues: Vec<Queue>,
    interrupt_cb: Arc<dyn VirtioInterrupt>,
    inflate_queue_evt: EventFd,
    deflate_queue_evt: EventFd,
    stats_queue_evt: Option<EventFd>,
    reporting_queue_evt: Option<EventFd>,
    hetero_inflate_queue_evt: Option<EventFd>,
    hetero_deflate_queue_evt: Option<EventFd>,
    kill_evt: EventFd,
    pause_evt: EventFd,
    pbp: Option<PartiallyBalloonedPage>,
    counters: Arc<BalloonCounters>,
}

impl BalloonEpollHandler {
    fn signal(&self, int_type: VirtioInterruptType) -> result::Result<(), Error> {
        self.interrupt_cb.trigger(int_type).map_err(|e| {
            error!("Failed to signal used queue: {:?}", e);
            Error::FailedSignal(e)
        })
    }

    fn advise_memory_range(
        memory: &GuestMemoryMmap,
        range_base: GuestAddress,
        range_len: usize,
        advice: libc::c_int,
    ) -> result::Result<(), Error> {
        let hva = memory
            .get_host_address(range_base)
            .map_err(Error::GuestMemory)?;
        let res =
            // SAFETY: Need unsafe to do syscall madvise
            unsafe { libc::madvise(hva as *mut libc::c_void, range_len as libc::size_t, advice) };
        if res != 0 {
            return Err(Error::MadviseFail(io::Error::last_os_error()));
        }
        Ok(())
    }

    fn release_memory_range(
        memory: &GuestMemoryMmap,
        range_base: GuestAddress,
        range_len: usize,
    ) -> result::Result<(), Error> {
        let region = memory.find_region(range_base).ok_or(Error::GuestMemory(
            GuestMemoryError::InvalidGuestAddress(range_base),
        ))?;
        if let Some(f_off) = region.file_offset() {
            let offset = range_base.0 - region.start_addr().0;
            // SAFETY: FFI call with valid arguments
            let res = unsafe {
                libc::fallocate64(
                    f_off.file().as_raw_fd(),
                    libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE,
                    (offset + f_off.start()) as libc::off64_t,
                    range_len as libc::off64_t,
                )
            };

            if res != 0 {
                return Err(Error::FallocateFail(io::Error::last_os_error()));
            }
        }

        Self::advise_memory_range(memory, range_base, range_len, libc::MADV_DONTNEED)
    }

    fn release_memory_range_4k(
        pbp: &mut Option<PartiallyBalloonedPage>,
        memory: &GuestMemoryMmap,
        pfn: u32,
    ) -> result::Result<(), Error> {
        let range_base = GuestAddress((pfn as u64) << VIRTIO_BALLOON_PFN_SHIFT);
        let range_len = 1 << VIRTIO_BALLOON_PFN_SHIFT;

        let page_size: u64 = get_page_size();
        if page_size == 1 << VIRTIO_BALLOON_PFN_SHIFT {
            return Self::release_memory_range(memory, range_base, range_len);
        }

        if pbp.is_none() {
            *pbp = Some(PartiallyBalloonedPage::new());
        }

        if !pbp.as_ref().unwrap().pfn_match(range_base.0) {
            // We are trying to free memory region in a different pfn with current pbp. Flush pbp.
            pbp.as_mut().unwrap().reset();
            pbp.as_mut().unwrap().addr = align_page_size_down(range_base.0);
        }

        pbp.as_mut().unwrap().set_bit(range_base.0);
        if pbp.as_ref().unwrap().bitmap_full() {
            Self::release_memory_range(
                memory,
                vm_memory::GuestAddress(pbp.as_ref().unwrap().addr),
                page_size as usize,
            )?;

            pbp.as_mut().unwrap().reset();
        }

        Ok(())
    }

    fn process_queue(&mut self, queue_index: usize) -> result::Result<(), Error> {
        let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let desc = desc_chain.next().ok_or(Error::DescriptorChainTooShort)?;

            let data_chunk_size = size_of::<u32>();

            // The head contains the request type which MUST be readable.
            if desc.is_write_only() {
                error!("The head contains the request type is not right");
                return Err(Error::UnexpectedWriteOnlyDescriptor);
            }
            if desc.len() as usize % data_chunk_size != 0 {
                error!("the request size {} is not right", desc.len());
                return Err(Error::InvalidRequest);
            }

            let mut offset = 0u64;
            while offset < desc.len() as u64 {
                let addr = desc.addr().checked_add(offset).unwrap();
                let pfn: u32 = desc_chain
                    .memory()
                    .read_obj(addr)
                    .map_err(Error::GuestMemory)?;
                offset += data_chunk_size as u64;

                if queue_index == 0
                    || (self.queues.len() >= 4 && queue_index == self.queues.len() - 2)
                {
                    Self::release_memory_range_4k(&mut self.pbp, desc_chain.memory(), pfn)?;
                } else if queue_index == 1
                    || (self.queues.len() >= 4 && queue_index == self.queues.len() - 1)
                {
                    let page_size = get_page_size() as usize;
                    let rbase = align_page_size_down((pfn as u64) << VIRTIO_BALLOON_PFN_SHIFT);

                    Self::advise_memory_range(
                        desc_chain.memory(),
                        vm_memory::GuestAddress(rbase),
                        page_size,
                        libc::MADV_WILLNEED,
                    )?;
                } else {
                    return Err(Error::InvalidQueueIndex(queue_index));
                }
            }

            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), desc.len())
                .map_err(Error::QueueAddUsed)?;
            used_descs = true;
        }

        if used_descs {
            self.signal(VirtioInterruptType::Queue(queue_index as u16))
        } else {
            Ok(())
        }
    }

    fn process_stats_queue(&mut self, queue_index: usize) -> result::Result<(), Error> {
        // let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let desc = desc_chain.next().ok_or(Error::DescriptorChainTooShort)?;

            #[repr(C, packed)]
            #[derive(Copy, Clone, Debug, Default, Versionize)]
            pub struct BalloonStat {
                tag: u16,
                val: u64,
            }
            // SAFETY: BalloonStat is a POB which does not contain any pointers
            unsafe impl ByteValued for BalloonStat {}

            let data_chunk_size = size_of::<BalloonStat>();

            // The head contains the request type which MUST be readable.
            if desc.is_write_only() {
                error!("The head contains the request type is not right");
                return Err(Error::UnexpectedWriteOnlyDescriptor);
            }
            if desc.len() as usize % data_chunk_size != 0 {
                error!("the request size {} is not right", desc.len());
                return Err(Error::InvalidRequest);
            }

            let mut offset = 0u64;
            while offset < desc.len() as u64 {
                let addr = desc.addr().checked_add(offset).unwrap();
                let stat: BalloonStat = desc_chain
                    .memory()
                    .read_obj(addr)
                    .map_err(Error::GuestMemory)?;
                offset += data_chunk_size as u64;
                match stat.tag {
                    0 => self.counters.swap_in.store(stat.val, Ordering::Release),
                    1 => self.counters.swap_out.store(stat.val, Ordering::Release),
                    2 => self
                        .counters
                        .major_faults
                        .store(stat.val, Ordering::Release),
                    3 => self
                        .counters
                        .minor_faults
                        .store(stat.val, Ordering::Release),
                    4 => self.counters.free_memory.store(stat.val, Ordering::Release),
                    5 => self
                        .counters
                        .total_memory
                        .store(stat.val, Ordering::Release),
                    6 => self
                        .counters
                        .available_memory
                        .store(stat.val, Ordering::Release),
                    7 => self.counters.disk_caches.store(stat.val, Ordering::Release),
                    8 => self
                        .counters
                        .hugetlb_allocations
                        .store(stat.val, Ordering::Release),
                    9 => self
                        .counters
                        .hugetlb_failures
                        .store(stat.val, Ordering::Release),
                    _ => return Err(Error::UnexpectedStatTag(stat.tag)),
                };
            }
            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), desc.len())
                .map_err(Error::QueueAddUsed)?;
            // used_descs = true;
        }

        // TODO: signal the Guest when we need to refresh statistics
        // if used_descs {
        //     self.signal(VirtioInterruptType::Queue(queue_index as u16))
        // } else {
        //     Ok(())
        // }
        Ok(())
    }

    fn process_reporting_queue(&mut self, queue_index: usize) -> result::Result<(), Error> {
        let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let mut descs_len = 0;
            while let Some(desc) = desc_chain.next() {
                descs_len += desc.len();
                Self::release_memory_range(desc_chain.memory(), desc.addr(), desc.len() as usize)?;
            }

            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), descs_len)
                .map_err(Error::QueueAddUsed)?;
            used_descs = true;
        }

        if used_descs {
            self.signal(VirtioInterruptType::Queue(queue_index as u16))
        } else {
            Ok(())
        }
    }

    fn run(
        &mut self,
        paused: Arc<AtomicBool>,
        paused_sync: Arc<Barrier>,
    ) -> result::Result<(), EpollHelperError> {
        let mut helper = EpollHelper::new(&self.kill_evt, &self.pause_evt)?;
        helper.add_event(self.inflate_queue_evt.as_raw_fd(), INFLATE_QUEUE_EVENT)?;
        helper.add_event(self.deflate_queue_evt.as_raw_fd(), DEFLATE_QUEUE_EVENT)?;
        if let Some(stats_queue_evt) = self.stats_queue_evt.as_ref() {
            helper.add_event(stats_queue_evt.as_raw_fd(), STATS_QUEUE_EVENT)?;
        }
        if let Some(reporting_queue_evt) = self.reporting_queue_evt.as_ref() {
            helper.add_event(reporting_queue_evt.as_raw_fd(), REPORTING_QUEUE_EVENT)?;
        }
        if let Some(hetero_inflate_queue_evt) = self.hetero_inflate_queue_evt.as_ref() {
            helper.add_event(
                hetero_inflate_queue_evt.as_raw_fd(),
                HETERO_INFLATE_QUEUE_EVENT,
            )?;
        }
        if let Some(hetero_deflate_queue_evt) = self.hetero_deflate_queue_evt.as_ref() {
            helper.add_event(
                hetero_deflate_queue_evt.as_raw_fd(),
                HETERO_DEFLATE_QUEUE_EVENT,
            )?;
        }
        helper.run(paused, paused_sync, self)?;

        Ok(())
    }
}

impl EpollHelperHandler for BalloonEpollHandler {
    fn handle_event(
        &mut self,
        _helper: &mut EpollHelper,
        event: &epoll::Event,
    ) -> result::Result<(), EpollHelperError> {
        let ev_type = event.data as u16;
        match ev_type {
            INFLATE_QUEUE_EVENT => {
                self.inflate_queue_evt.read().map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to get inflate queue event: {:?}",
                        e
                    ))
                })?;
                self.process_queue(0).map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to signal used inflate queue: {:?}",
                        e
                    ))
                })?;
            }
            DEFLATE_QUEUE_EVENT => {
                self.deflate_queue_evt.read().map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to get deflate queue event: {:?}",
                        e
                    ))
                })?;
                self.process_queue(1).map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to signal used deflate queue: {:?}",
                        e
                    ))
                })?;
            }
            STATS_QUEUE_EVENT => {
                if let Some(stats_queue_evt) = self.stats_queue_evt.as_ref() {
                    stats_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get statistics queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_stats_queue(2).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used statistics queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid statistics queue event as no eventfd registered"
                    )));
                }
            }
            REPORTING_QUEUE_EVENT => {
                if let Some(reporting_queue_evt) = self.reporting_queue_evt.as_ref() {
                    reporting_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get reporting queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_reporting_queue(
                        2 + self.stats_queue_evt.as_ref().map(|_| 1).unwrap_or(0),
                    )
                    .map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used reporting queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid reporting queue event as no eventfd registered"
                    )));
                }
            }
            HETERO_INFLATE_QUEUE_EVENT => {
                if let Some(hetero_inflate_queue_evt) = self.hetero_inflate_queue_evt.as_ref() {
                    hetero_inflate_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get heterogeneous inflate queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_queue(self.queues.len() - 2).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used heterogeneous inflate queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid heterogeneous inflate queue event as no eventfd registered"
                    )));
                }
            }
            HETERO_DEFLATE_QUEUE_EVENT => {
                if let Some(hetero_deflate_queue_evt) = self.hetero_deflate_queue_evt.as_ref() {
                    hetero_deflate_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get heterogeneous deflate queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_queue(self.queues.len() - 1).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used heterogeneous deflate queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid heterogeneous deflate queue event as no eventfd registered"
                    )));
                }
            }
            _ => {
                return Err(EpollHelperError::HandleEvent(anyhow!(
                    "Unknown event for virtio-balloon"
                )));
            }
        }

        Ok(())
    }
}

#[derive(Versionize)]
pub struct BalloonState {
    pub avail_features: u64,
    pub acked_features: u64,
    pub config: VirtioBalloonConfig,
}

impl VersionMapped for BalloonState {}

// Virtio device for exposing entropy to the guest OS through virtio.
pub struct Balloon {
    common: VirtioCommon,
    id: String,
    config: VirtioBalloonConfig,
    seccomp_action: SeccompAction,
    exit_evt: EventFd,
    interrupt_cb: Option<Arc<dyn VirtioInterrupt>>,
    counters: Arc<BalloonCounters>,
}

impl Balloon {
    // Create a new virtio-balloon.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: String,
        size: [u64; 2],
        statistics: bool,
        deflate_on_oom: bool,
        free_page_reporting: bool,
        heterogeneous_memory: bool,
        seccomp_action: SeccompAction,
        exit_evt: EventFd,
        state: Option<BalloonState>,
    ) -> io::Result<Self> {
        let mut queue_sizes = vec![QUEUE_SIZE; MIN_NUM_QUEUES];

        let (avail_features, acked_features, config, paused) = if let Some(state) = state {
            info!("Restoring virtio-balloon {}", id);
            (
                state.avail_features,
                state.acked_features,
                state.config,
                true,
            )
        } else {
            let mut avail_features = 1u64 << VIRTIO_F_VERSION_1;
            if statistics {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_STATS_VQ;
            }
            if deflate_on_oom {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_DEFLATE_ON_OOM;
            }
            if free_page_reporting {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_REPORTING;
            }
            if heterogeneous_memory {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_HETERO_MEM;
            }

            let config = VirtioBalloonConfig {
                num_pages: (size[0] >> VIRTIO_BALLOON_PFN_SHIFT) as u32,
                num_hetero_pages: (size[1] >> VIRTIO_BALLOON_PFN_SHIFT) as u32,
                ..Default::default()
            };

            (avail_features, 0, config, false)
        };

        if statistics {
            queue_sizes.push(REPORTING_QUEUE_SIZE);
        }
        if free_page_reporting {
            queue_sizes.push(REPORTING_QUEUE_SIZE);
        }
        if heterogeneous_memory {
            queue_sizes.extend_from_slice(&[QUEUE_SIZE; 2]);
        }

        Ok(Balloon {
            common: VirtioCommon {
                device_type: VirtioDeviceType::Balloon as u32,
                avail_features,
                acked_features,
                paused_sync: Some(Arc::new(Barrier::new(2))),
                queue_sizes,
                min_queues: MIN_NUM_QUEUES as u16,
                paused: Arc::new(AtomicBool::new(paused)),
                ..Default::default()
            },
            id,
            config,
            seccomp_action,
            exit_evt,
            interrupt_cb: None,
            counters: Arc::new(BalloonCounters::default()),
        })
    }

    pub fn resize(&mut self, size: [u64; 2]) -> Result<(), Error> {
        self.config.num_pages = (size[0] >> VIRTIO_BALLOON_PFN_SHIFT) as u32;
        self.config.num_hetero_pages = (size[1] >> VIRTIO_BALLOON_PFN_SHIFT) as u32;

        if let Some(interrupt_cb) = &self.interrupt_cb {
            interrupt_cb
                .trigger(VirtioInterruptType::Config)
                .map_err(Error::FailedSignal)
        } else {
            Ok(())
        }
    }

    // Get the actual size of the virtio-balloon.
    pub fn get_actual(&self) -> u64 {
        (self.config.actual as u64) << VIRTIO_BALLOON_PFN_SHIFT
    }

    fn state(&self) -> BalloonState {
        BalloonState {
            avail_features: self.common.avail_features,
            acked_features: self.common.acked_features,
            config: self.config,
        }
    }

    #[cfg(fuzzing)]
    pub fn wait_for_epoll_threads(&mut self) {
        self.common.wait_for_epoll_threads();
    }
}

impl Drop for Balloon {
    fn drop(&mut self) {
        if let Some(kill_evt) = self.common.kill_evt.take() {
            // Ignore the result because there is nothing we can do about it.
            let _ = kill_evt.write(1);
        }
        self.common.wait_for_epoll_threads();
    }
}

impl VirtioDevice for Balloon {
    fn device_type(&self) -> u32 {
        self.common.device_type
    }

    fn queue_max_sizes(&self) -> &[u16] {
        &self.common.queue_sizes
    }

    fn features(&self) -> u64 {
        self.common.avail_features
    }

    fn ack_features(&mut self, value: u64) {
        self.common.ack_features(value)
    }

    fn read_config(&self, offset: u64, data: &mut [u8]) {
        self.read_config_from_slice(self.config.as_slice(), offset, data);
    }

    fn write_config(&mut self, offset: u64, data: &[u8]) {
        // The "actual" and "hetero_actual" fields are the only mutable fields
        if (offset != CONFIG_ACTUAL_OFFSET && offset != CONFIG_HETERO_ACTUAL_OFFSET)
            || data.len() != CONFIG_ACTUAL_SIZE
        {
            error!(
                "Attempt to write to read-only field: offset {:x} length {}",
                offset,
                data.len()
            );
            return;
        }

        let config = self.config.as_mut_slice();
        let config_len = config.len() as u64;
        let data_len = data.len() as u64;
        if offset + data_len > config_len {
            error!(
                    "Out-of-bound access to configuration: config_len = {} offset = {:x} length = {} for {}",
                    config_len,
                    offset,
                    data_len,
                    self.device_type()
                );
            return;
        }

        if let Some(end) = offset.checked_add(config.len() as u64) {
            let mut offset_config =
                &mut config[offset as usize..std::cmp::min(end, config_len) as usize];
            offset_config.write_all(data).unwrap();
        }
    }

    fn activate(
        &mut self,
        mem: GuestMemoryAtomic<GuestMemoryMmap>,
        interrupt_cb: Arc<dyn VirtioInterrupt>,
        mut queues: Vec<(usize, Queue, EventFd)>,
    ) -> ActivateResult {
        self.common.activate(&queues, &interrupt_cb)?;
        let (kill_evt, pause_evt) = self.common.dup_eventfds();

        let mut virtqueues = Vec::new();
        let (_, queue, queue_evt) = queues.remove(0);
        virtqueues.push(queue);
        let inflate_queue_evt = queue_evt;
        let (_, queue, queue_evt) = queues.remove(0);
        virtqueues.push(queue);
        let deflate_queue_evt = queue_evt;
        let stats_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_STATS_VQ) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };
        let reporting_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_REPORTING) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };
        let hetero_inflate_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_HETERO_MEM) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };
        let hetero_deflate_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_HETERO_MEM) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };

        self.interrupt_cb = Some(interrupt_cb.clone());

        let mut handler = BalloonEpollHandler {
            mem,
            queues: virtqueues,
            interrupt_cb,
            inflate_queue_evt,
            deflate_queue_evt,
            stats_queue_evt,
            reporting_queue_evt,
            hetero_inflate_queue_evt,
            hetero_deflate_queue_evt,
            kill_evt,
            pause_evt,
            pbp: None,
            counters: self.counters.clone(),
        };

        let paused = self.common.paused.clone();
        let paused_sync = self.common.paused_sync.clone();
        let mut epoll_threads = Vec::new();

        spawn_virtio_thread(
            &self.id,
            &self.seccomp_action,
            Thread::VirtioBalloon,
            &mut epoll_threads,
            &self.exit_evt,
            move || handler.run(paused, paused_sync.unwrap()),
        )?;
        self.common.epoll_threads = Some(epoll_threads);

        event!("virtio-device", "activated", "id", &self.id);
        Ok(())
    }

    fn reset(&mut self) -> Option<Arc<dyn VirtioInterrupt>> {
        let result = self.common.reset();
        event!("virtio-device", "reset", "id", &self.id);
        result
    }

    fn counters(&self) -> Option<HashMap<&'static str, Wrapping<u64>>> {
        Some(HashMap::from([
            (
                "swap_in",
                Wrapping(self.counters.swap_in.load(Ordering::Acquire)),
            ),
            (
                "swap_out",
                Wrapping(self.counters.swap_out.load(Ordering::Acquire)),
            ),
            (
                "major_faults",
                Wrapping(self.counters.major_faults.load(Ordering::Acquire)),
            ),
            (
                "minor_faults",
                Wrapping(self.counters.minor_faults.load(Ordering::Acquire)),
            ),
            (
                "free_memory",
                Wrapping(self.counters.free_memory.load(Ordering::Acquire)),
            ),
            (
                "total_memory",
                Wrapping(self.counters.total_memory.load(Ordering::Acquire)),
            ),
            (
                "available_memory",
                Wrapping(self.counters.available_memory.load(Ordering::Acquire)),
            ),
            (
                "disk_caches",
                Wrapping(self.counters.disk_caches.load(Ordering::Acquire)),
            ),
            (
                "hugetlb_allocations",
                Wrapping(self.counters.hugetlb_allocations.load(Ordering::Acquire)),
            ),
            (
                "hugetlb_failures",
                Wrapping(self.counters.hugetlb_failures.load(Ordering::Acquire)),
            ),
        ]))
    }
}

impl Pausable for Balloon {
    fn pause(&mut self) -> result::Result<(), MigratableError> {
        self.common.pause()
    }

    fn resume(&mut self) -> result::Result<(), MigratableError> {
        self.common.resume()
    }
}

impl Snapshottable for Balloon {
    fn id(&self) -> String {
        self.id.clone()
    }

    fn snapshot(&mut self) -> std::result::Result<Snapshot, MigratableError> {
        Snapshot::new_from_versioned_state(&self.state())
    }
}
impl Transportable for Balloon {}
impl Migratable for Balloon {}

#[derive(Debug, Default)]
pub struct BalloonCounters {
    swap_in: AtomicU64,
    swap_out: AtomicU64,
    major_faults: AtomicU64,
    minor_faults: AtomicU64,
    free_memory: AtomicU64,
    total_memory: AtomicU64,
    available_memory: AtomicU64,
    disk_caches: AtomicU64,
    hugetlb_allocations: AtomicU64,
    hugetlb_failures: AtomicU64,
}
