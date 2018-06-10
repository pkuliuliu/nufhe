import numpy

from reikna.core import Computation, Transformation, Parameter, Annotation, Type
from reikna.fft import FFT
from reikna.cluda import dtypes, functions

from .numeric_functions import Torus32, float_to_int32


def transformed_dtype():
    return numpy.dtype('complex128')


def transformed_length(N):
    return N // 2


def forward_transform_ref(data):
    batch_shape = data.shape[:-1]
    N = data.shape[-1]
    a = data.reshape(numpy.prod(batch_shape), N)
    idxs = numpy.arange(N//2)
    in_arr = (a[:,:N//2] - 1j * a[:,N//2:]) * numpy.exp(-2j * numpy.pi * idxs / N / 2)
    return numpy.fft.fft(in_arr).reshape(batch_shape + (N//2,))


def inverse_transform_ref(data):
    batch_shape = data.shape[:-1]
    N = data.shape[-1] * 2
    a = data.reshape(numpy.prod(batch_shape), N//2)
    out_arr = numpy.fft.ifft(a)
    idxs = numpy.arange(N//2)
    out_arr = out_arr.conj() * numpy.exp(-2j * numpy.pi * idxs / N / 2)
    return numpy.concatenate([
        float_to_int32(out_arr.real),
        float_to_int32(out_arr.imag)], axis=1).reshape(batch_shape + (N,))


def transformed_space_add_ref(data1, data2):
    return data1 + data2


def transformed_space_mul_ref(data1, data2):
    return data1 * data2


def transformed_add():
    return functions.add(transformed_dtype(), transformed_dtype())

def transformed_mul():
    return functions.mul(transformed_dtype(), transformed_dtype())


def process_input(oarr, iarr, carr):
    N = iarr.shape[-1]
    return Transformation(
        [
            Parameter('output', Annotation(oarr, 'o')),
            Parameter('input', Annotation(iarr, 'i')),
            Parameter('coeffs', Annotation(carr, 'i')),
        ],
        """
        ${input.ctype} x_re = ${input.load_idx}(${", ".join(idxs[:-1])}, ${idxs[-1]});
        ${input.ctype} x_im = ${input.load_idx}(${", ".join(idxs[:-1])}, ${idxs[-1]} + ${N//2});

        ${output.ctype} x = COMPLEX_CTR(${output.ctype})(x_re, -x_im);
        ${output.ctype} coeff = ${coeffs.load_idx}(${idxs[-1]});
        ${output.store_same}(${mul}(x, coeff));
        """,
        connectors=['output'],
        render_kwds=dict(
            N=N,
            polar=functions.polar_unit(dtypes.real_for(oarr.dtype)),
            mul=functions.mul(oarr.dtype, oarr.dtype)))


class ForwardTransformFFT(Computation):
    """
    FFT with pre/post-processing designed for polynomial multiplication.
    """

    def __init__(self, batch_shape, polynomial_size):
        tdtype = transformed_dtype()
        iarr_t = Type(dtypes.real_for(tdtype), batch_shape + (polynomial_size,))
        oarr_t = Type(tdtype, batch_shape + (transformed_length(polynomial_size),))
        Computation.__init__(self, [
            Parameter('output', Annotation(oarr_t, 'o')),
            Parameter('input', Annotation(iarr_t, 'i'))])

    def _build_plan(self, plan_factory, device_params, output, input_):

        plan = plan_factory()

        fft = FFT(output, axes=(len(input_.shape) - 1,))

        N = input_.shape[-1]
        coeffs = plan.persistent_array(
            numpy.exp(-2j * numpy.pi * numpy.arange(N // 2) / 2 / N).astype(fft.parameter.input.dtype))

        process = process_input(output, input_, coeffs)
        fft.parameter.input.connect(
            process, process.output, r_input=process.input, coeffs=process.coeffs)

        plan.computation_call(fft, output, input_, coeffs)

        return plan


def process_output(oarr, iarr, carr):
    N = oarr.shape[-1]
    return Transformation(
        [
            Parameter('output', Annotation(oarr, 'o')),
            Parameter('input', Annotation(iarr, 'i')),
            Parameter('coeffs', Annotation(carr, 'i')),
        ],
        """
        ${input.ctype} x = ${input.load_same};
        ${input.ctype} coeff = ${coeffs.load_idx}(${idxs[-1]});
        ${input.ctype} res = ${mul}(${conj}(x), coeff);
        ${output.store_idx}(${", ".join(idxs[:-1])}, ${idxs[-1]}, res.x);
        ${output.store_idx}(${", ".join(idxs[:-1])}, ${idxs[-1]} + ${N//2}, res.y);
        """,
        connectors=['input'],
        render_kwds=dict(
            N=N,
            polar=functions.polar_unit(dtypes.real_for(iarr.dtype)),
            mul=functions.mul(iarr.dtype, iarr.dtype),
            conj=functions.conj(iarr.dtype)))


class InverseTransformFFT(Computation):
    """
    IFFT with pre/post-processing designed for polynomial multiplication.
    """

    def __init__(self, batch_shape, polynomial_size):
        tdtype = transformed_dtype()
        iarr_t = Type(tdtype, batch_shape + (transformed_length(polynomial_size),))
        oarr_t = Type(dtypes.real_for(tdtype), batch_shape + (polynomial_size,))
        Computation.__init__(self, [
            Parameter('output', Annotation(oarr_t, 'o')),
            Parameter('input', Annotation(iarr_t, 'i'))])

    def _build_plan(self, plan_factory, device_params, output, input_):

        plan = plan_factory()

        fft = FFT(input_, axes=(len(input_.shape) - 1,))

        N = input_.shape[-1] * 2
        coeffs = plan.persistent_array(
            numpy.exp(-2j * numpy.pi * numpy.arange(N // 2) / 2 / N).astype(fft.parameter.output.dtype))

        process = process_output(output, input_, coeffs)
        fft.parameter.output.connect(
            process, process.input, r_output=process.output, coeffs=process.coeffs)

        plan.computation_call(fft, output, coeffs, input_, inverse=True)

        return plan


def from_integer(shape, input_dtype, output_dtype):
    return Transformation(
        [
            Parameter('output', Annotation(Type(output_dtype, shape), 'o')),
            Parameter('input', Annotation(Type(input_dtype, shape), 'i')),
        ],
        """
        ${output.store_same}((${output.ctype})(${input.load_same}));
        """,
        connectors=['output', 'input'])


class ForwardTransform(Computation):
    """
    I2C FFT using pre/post-processing.
    """

    def __init__(self, batch_shape, polynomial_size):
        input_dtype = Torus32

        fft = ForwardTransformFFT(batch_shape, polynomial_size)

        tr_input = from_integer(fft.parameter.input.shape, input_dtype, fft.parameter.input.dtype)

        fft.parameter.input.connect(
            tr_input, tr_input.output, input_poly=tr_input.input)

        self._fft = fft

        Computation.__init__(self, [
            Parameter('output', Annotation(fft.parameter.output, 'o')),
            Parameter('input', Annotation(fft.parameter.input_poly, 'i'))])

    def _build_plan(self, plan_factory, device_params, output, input_):
        plan = plan_factory()
        plan.computation_call(self._fft, output, input_)
        return plan


def to_integer(shape, input_dtype, output_dtype):
    return Transformation(
        [
            Parameter('output', Annotation(Type(output_dtype, shape), 'o')),
            Parameter('input', Annotation(Type(input_dtype, shape), 'i')),
        ],
        """
        // The result is within the range of int64, so it must be first
        // converted to integer and then taken modulo 2^32
        ${output.store_same}(
            (${out_ctype})((${i64_ctype})(round(${input.load_same})))
        );
        """,
        render_kwds=dict(
            out_ctype=dtypes.ctype(output_dtype),
            i64_ctype=dtypes.ctype(numpy.int64)),
        connectors=['input'])


class InverseTransform(Computation):

    def __init__(self, batch_shape, polynomial_size):

        output_dtype = Torus32

        fft = InverseTransformFFT(batch_shape, polynomial_size)

        tr_output = to_integer(fft.parameter.output.shape, fft.parameter.output.dtype, output_dtype)

        fft.parameter.output.connect(
            tr_output, tr_output.input, output_poly=tr_output.output)

        self._fft = fft

        Computation.__init__(self, [
            Parameter('output', Annotation(fft.parameter.output_poly, 'o')),
            Parameter('input', Annotation(fft.parameter.input, 'i'))])

    def _build_plan(self, plan_factory, device_params, output, input_):
        plan = plan_factory()
        plan.computation_call(self._fft, output, input_)
        return plan

