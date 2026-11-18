#include <mkl.h>
#include <mkl_vsl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

_Static_assert(sizeof(MKL_INT) == 4, "This test uses the MKL LP64 interface");
/* Intel's 2022.1.0 package identifies its C API as 2022.0 update 1. */
#if INTEL_MKL_VERSION != 20220001
#error "The compiler SDK requires Intel MKL 2022.1.0"
#endif

static int check_random_streams(void) {
    enum { count = 64 };
    struct samples {
        double before, values[count], after;
    } a = {.before = 1234, .after = 5678}, b = {.before = 1234, .after = 5678};
    VSLStreamStatePtr left = NULL, right = NULL;
    int failed = 1;
    if (vslNewStream(&left, VSL_BRNG_MT19937, 42) != VSL_STATUS_OK)
        goto done;
    if (vslNewStream(&right, VSL_BRNG_MT19937, 42) != VSL_STATUS_OK)
        goto done;
    if (vdRngUniform(VSL_RNG_METHOD_UNIFORM_STD, left, count, a.values, 0, 1) != VSL_STATUS_OK ||
        vdRngUniform(VSL_RNG_METHOD_UNIFORM_STD, right, count, b.values, 0, 1) != VSL_STATUS_OK)
        goto done;
    if (a.before != 1234 || b.before != 1234 || a.after != 5678 || b.after != 5678 ||
        memcmp(a.values, b.values, sizeof(a.values)) != 0)
        goto done;
    int varied = 0;
    for (int i = 0; i < count; ++i) {
        if (!isfinite(a.values[i]) || a.values[i] < 0 || a.values[i] >= 1)
            goto done;
        varied |= a.values[i] != a.values[0];
    }
    failed = !varied;
done:
    if (left && vslDeleteStream(&left) != VSL_STATUS_OK)
        failed = 1;
    if (right && vslDeleteStream(&right) != VSL_STATUS_OK)
        failed = 1;
    return failed;
}

int main(void) {
    MKLVersion version;
    mkl_get_version(&version);
    if (version.MajorVersion != __INTEL_MKL__ || version.MinorVersion != __INTEL_MKL_MINOR__ ||
        version.UpdateVersion != __INTEL_MKL_UPDATE__)
        return 1;
    void *buffer = mkl_malloc(256, 64);
    if (!buffer)
        return 2;
    int failed = ((uintptr_t)buffer % 64) != 0;
    memset(buffer, 0x5a, 256);
    mkl_free(buffer);
    if (failed)
        return 3;
    const double a[4] = {1, 2, 3, 4}, b[4] = {5, 6, 7, 8}, expected[4] = {19, 22, 43, 50};
    double result[4] = {0};
    cblas_dgemm(CblasRowMajor, CblasNoTrans, CblasNoTrans, 2, 2, 2,
                1, a, 2, b, 2, 0, result, 2);
    for (int i = 0; i < 4; ++i)
        if (!isfinite(result[i]) || fabs(result[i] - expected[i]) > 1e-12)
            return 4;
    failed = check_random_streams();
    mkl_free_buffers();
    printf("MKL %d.%d update %d: aligned allocation, BLAS and VSL %s\n",
           version.MajorVersion, version.MinorVersion, version.UpdateVersion, failed ? "FAILED" : "OK");
    return failed ? 5 : 0;
}
