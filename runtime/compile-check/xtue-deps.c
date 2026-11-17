#include <nghttp2/nghttp2.h>
#include <libxslt/xslt.h>
#include <infiniband/verbs.h>
#include <gperftools/profiler.h>
#include <forms.h>
#include <cblas.h>

extern void dgesv_(int *, int *, double *, int *, int *, double *, int *, int *);
/* Retain real references without opening an X display or RDMA device. */
static __typeof__(&fl_initialize) volatile forms_api = &fl_initialize;
static __typeof__(&ibv_get_device_list) volatile verbs_api = &ibv_get_device_list;
static __typeof__(&ProfilerStart) volatile profiler_api = &ProfilerStart;
int main(void) {
    int n=1, rhs=1, leading=1, pivot=0, info=-1;
    double matrix=2, result=6, a[2]={1,2}, b[2]={3,4};
    dgesv_(&n,&rhs,&matrix,&leading,&pivot,&result,&leading,&info);
    return info || result != 3 || cblas_ddot(2,a,1,b,1) != 11 ||
        !nghttp2_version(0) || !xsltEngineVersion || !forms_api || !verbs_api || !profiler_api;
}
