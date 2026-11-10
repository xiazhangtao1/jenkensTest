#include <hwloc.h>
#include <stdio.h>

int main(void)
{
    hwloc_topology_t topology;
    if (hwloc_topology_init(&topology) < 0) {
        perror("hwloc_topology_init");
        return 1;
    }
    /* A synthetic topology exercises the API without binding or host CPU assumptions. */
    if (hwloc_topology_set_synthetic(topology, "package:2 core:2 pu:2") < 0 ||
        hwloc_topology_load(topology) < 0) {
        perror("hwloc topology load");
        hwloc_topology_destroy(topology);
        return 1;
    }
    int count = hwloc_get_nbobjs_by_type(topology, HWLOC_OBJ_PU);
    hwloc_topology_destroy(topology);
    if (count != 8) {
        fprintf(stderr, "Expected 8 processing units, got %d\n", count);
        return 1;
    }
    printf("hwloc API 0x%x: synthetic topology has %d processing units\n",
           hwloc_get_api_version(), count);
    return 0;
}
