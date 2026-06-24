#ifndef OFFSET_H
#define OFFSET_H

#include <stdint.h>
#include <stddef.h>

/* Offset calculator state */
typedef struct {
    uint64_t total_size;     /* Total addressable space in bytes */
    uint64_t block_size;     /* Underlying block device block size */
} OffsetConfig;

/* Result type for offset calculations */
typedef struct {
    uint64_t offset;         /* Calculated byte offset */
    int valid;               /* 1 if valid, 0 if out of bounds */
} OffsetResult;

/**
 * Initialize offset calculator with total size and block size
 * Returns 1 on success, 0 on invalid config
 */
int offset_config_init(OffsetConfig *config, uint64_t total_size, uint64_t block_size);

/**
 * Calculate byte offset from position
 * Position is normalized: 0 <= position < total_size
 * Returns OffsetResult with valid=1 if in bounds, valid=0 otherwise
 */
OffsetResult offset_calculate(const OffsetConfig *config, uint64_t position);

/**
 * Validate that offset is aligned to block boundary
 * Returns 1 if aligned, 0 otherwise
 */
int offset_is_aligned(const OffsetConfig *config, uint64_t offset);

#endif /* OFFSET_H */
