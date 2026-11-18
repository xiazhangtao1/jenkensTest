#include <nghttp2/nghttp2.h>
#include <libxslt/xslt.h>
#include <infiniband/verbs.h>
#include <gperftools/profiler.h>
#include <forms.h>
#include <cblas.h>
#include <lapacke.h>
#include <lapacke_utils.h>
#include <math.h>

/* Retain real references without opening an X display or RDMA device. */
static __typeof__(&fl_initialize) volatile forms_api = &fl_initialize;
static __typeof__(&ibv_get_device_list) volatile verbs_api = &ibv_get_device_list;
static __typeof__(&ProfilerStart) volatile profiler_api = &ProfilerStart;
int main(void) {
    lapack_int n=1, rhs=1, leading=1, pivot=0, info=-1;
    double matrix=2, result=6, a[2]={1,2}, b[2]={3,4};
    dgesv_(&n,&rhs,&matrix,&leading,&pivot,&result,&leading,&info);
    double coefficients[4]={3,1,1,2}, solution[2]={9,8};
    lapack_int pivots[2];
    lapack_int lapacke_info = LAPACKE_dgesv(LAPACK_ROW_MAJOR,2,1,coefficients,2,pivots,solution,1);
    return info || result != 3 || cblas_ddot(2,a,1,b,1) != 11 ||
        lapacke_info || fabs(solution[0]-2) > 1e-12 || fabs(solution[1]-3) > 1e-12 ||
        !nghttp2_version(0) || !xsltEngineVersion || !forms_api || !verbs_api || !profiler_api;
}
