from libcpp.vector cimport vector


cdef struct RampIndex:
    int start
    int end


cdef struct RampFit:
    float slope
    float read_var
    float poisson_var


ctypedef vector[RampIndex] RampQueue


cpdef RampQueue init_ramps(int[:, :] dq, int n_resultants, int index_pixel)
cdef RampFit fit_ramp(float[:] resultants_,
                      float[:] t_bar_,
                      float[:] tau_,
                      int[:] n_reads,
                      float read_noise,
                      RampIndex ramp)