#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>

#include <rte_eal.h>
#include <rte_lcore.h>

_Static_assert(RTE_MAX_LCORE == 256, "DPDK must support 256 logical lcores");

int main(int argc, char **argv)
{
    char *end;
    unsigned long expected;
    unsigned int current;

    if (argc < 3)
        return 2;
    errno = 0;
    expected = strtoul(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || expected > UINT_MAX)
        return 2;

    /* Keep EAL isolated from other SDK tests and exercise its own bounds check. */
    argv[1] = argv[0];
    if (rte_eal_init(argc - 1, argv + 1) < 0) {
        fputs("DPDK_EAL_INIT_FAILED\n", stderr);
        return 1;
    }
    current = rte_lcore_id();
    printf("RTE_MAX_LCORE=%u active_lcore=%u\n", (unsigned int)RTE_MAX_LCORE, current);
    if (current != expected || rte_lcore_count() != 1 ||
        expected >= RTE_MAX_LCORE || !rte_lcore_is_enabled((unsigned int)expected)) {
        rte_eal_cleanup();
        return 2;
    }
    return rte_eal_cleanup() == 0 ? 0 : 2;
}
