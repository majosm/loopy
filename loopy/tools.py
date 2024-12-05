__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import collections.abc as abc
import logging
from functools import cached_property
from sys import intern
from typing import List, FrozenSet, Generic, TypeVar, Iterator
from pyrsistent import PMap, pmap
from dataclasses import dataclass

import numpy as np
from immutables import Map

import islpy as isl
from pytools import ProcessLogger, memoize_method
from pytools.persistent_dict import (
    KeyBuilder as KeyBuilderBase,
    WriteOncePersistentDict,
)

from .symbolic import (
    RuleAwareIdentityMapper,
)
from .typing import is_integer  # noqa: F401


logger = logging.getLogger(__name__)


def update_persistent_hash(obj, key_hash, key_builder):
    """
    Custom hash computation function for use with
    :class:`pytools.persistent_dict.PersistentDict`.

    Only works in conjunction with :class:`loopy.tools.KeyBuilder`.
    """
    for field_name in obj.hash_fields:
        key_builder.rec(key_hash, getattr(obj, field_name))


# {{{ custom KeyBuilder subclass

class LoopyKeyBuilder(KeyBuilderBase):
    """A custom :class:`pytools.persistent_dict.KeyBuilder` subclass
    for objects within :mod:`loopy`.
    """

    # Lists, sets and dicts aren't immutable. But loopy kernels are, so we're
    # simply ignoring that fact here.
    update_for_list = KeyBuilderBase.update_for_tuple
    update_for_set = KeyBuilderBase.update_for_frozenset

    update_for_dict = KeyBuilderBase.update_for_immutabledict
    update_for_defaultdict = KeyBuilderBase.update_for_immutabledict

    def update_for_BasicSet(self, key_hash, key):  # noqa
        from islpy import Printer
        prn = Printer.to_str(key.get_ctx())
        getattr(prn, "print_"+key._base_name)(key)
        key_hash.update(prn.get_str().encode("utf8"))

    def update_for_Map(self, key_hash, key):  # noqa
        if isinstance(key, Map):
            self.update_for_dict(key_hash, key)
        elif isinstance(key, isl.Map):
            self.update_for_BasicSet(key_hash, key)
        else:
            raise AssertionError()

    update_for_PMap = update_for_dict  # noqa: N815

# }}}


# {{{ eq key builder

class LoopyEqKeyBuilder:
    """Unlike :class:`loopy.tools.LoopyKeyBuilder`, this builds keys for use in
    equality comparison, such that `key(a) == key(b)` if and only if `a == b`.
    The types of objects being compared should satisfy structural equality.

    The output is suitable for use with :class:`loopy.tools.LoopyKeyBuilder`
    provided all fields are persistent hashable.

    As an optimization, top-level pymbolic expression fields are stringified for
    faster comparisons / hash calculations.

    Usage::

        kb = LoopyEqKeyBuilder()
        kb.update_for_class(insn.__class__)
        kb.update_for_field("field", insn.field)
        ...
        key = kb.key()

    """

    def __init__(self):
        self.field_dict = {}

    def update_for_class(self, class_):
        self.class_ = class_

    def update_for_field(self, field_name, value):
        self.field_dict[field_name] = value

    def key(self):
        """A key suitable for equality comparison."""
        return (self.class_.__name__.encode("utf-8"), self.field_dict)

    @memoize_method
    def hash_key(self):
        """A key suitable for hashing.
        """
        # To speed up any calculations that repeatedly use the return value,
        # this method returns a hash.

        kb = LoopyKeyBuilder()
        # Build the key. For faster hashing, avoid hashing field names.
        key = (
            (self.class_.__name__.encode("utf-8"),
                *(self.field_dict[k] for k in sorted(self.field_dict.keys()))))

        return kb(key)

# }}}


# {{{ remove common indentation

def remove_common_indentation(code, require_leading_newline=True,
        ignore_lines_starting_with=None, strip_empty_lines=True):
    if "\n" not in code:
        return code

    # accommodate pyopencl-ish syntax highlighting
    cl_prefix = "//CL//"
    if code.startswith(cl_prefix):
        code = code[len(cl_prefix):]

    if require_leading_newline and not code.startswith("\n"):
        return code

    lines = code.split("\n")

    if strip_empty_lines:
        while lines[0].strip() == "":
            lines.pop(0)
        while lines[-1].strip() == "":
            lines.pop(-1)

    test_line = None
    if ignore_lines_starting_with:
        for line in lines:
            strip_l = line.lstrip()
            if (strip_l
                    and not strip_l.startswith(ignore_lines_starting_with)):
                test_line = line
                break

    else:
        test_line = lines[0]

    base_indent = 0
    if test_line:
        while test_line[base_indent] in " \t":
            base_indent += 1

    new_lines = []
    for line in lines:
        if (ignore_lines_starting_with
                and line.lstrip().startswith(ignore_lines_starting_with)):
            new_lines.append(line)
            continue

        if line[:base_indent].strip():
            raise ValueError("inconsistent indentation: '%s'" % line)

        new_lines.append(line[base_indent:])

    return "\n".join(new_lines)

# }}}


# {{{ remove_lines_with_only_spaces

def remove_lines_with_only_spaces(code):
    return "\n".join(line for line in code.split("\n") if set(line) != {" "})

# }}}


# {{{ build_ispc_shared_lib

# DO NOT RELY ON THESE: THEY WILL GO AWAY

def build_ispc_shared_lib(
        cwd, ispc_sources, cxx_sources,
        ispc_options=None, cxx_options=None,
        ispc_bin="ispc",
        cxx_bin="g++",
        quiet=True):
    if ispc_options is None:
        ispc_options = []
    if cxx_options is None:
        cxx_options = []

    from os.path import join

    ispc_source_names = []
    for name, contents in ispc_sources:
        ispc_source_names.append(name)

        with open(join(cwd, name), "w") as srcf:
            srcf.write(contents)

    cxx_source_names = []
    for name, contents in cxx_sources:
        cxx_source_names.append(name)

        with open(join(cwd, name), "w") as srcf:
            srcf.write(contents)

    from subprocess import check_call

    ispc_cmd = ([ispc_bin, "--pic", "-o", "ispc.o", *ispc_options, *ispc_source_names])
    if not quiet:
        print(" ".join(ispc_cmd))

    check_call(ispc_cmd, cwd=cwd)

    cxx_cmd = ([cxx_bin, "-shared", "-Wl,--export-dynamic", "-fPIC", "-oshared.so",
        "ispc.o", *cxx_options, *cxx_source_names])

    check_call(cxx_cmd, cwd=cwd)

    if not quiet:
        print(" ".join(cxx_cmd))

# }}}


# {{{ numpy address munging

# DO NOT RELY ON THESE: THEY WILL GO AWAY

def address_from_numpy(obj):
    ary_intf = getattr(obj, "__array_interface__", None)
    if ary_intf is None:
        raise RuntimeError("no array interface")

    buf_base, _is_read_only = ary_intf["data"]
    return buf_base + ary_intf.get("offset", 0)


def cptr_from_numpy(obj):
    import ctypes
    return ctypes.c_void_p(address_from_numpy(obj))


# https://github.com/hgomersall/pyFFTW/blob/master/pyfftw/utils.pxi#L172
def empty_aligned(shape, dtype, order="C", n=64):
    """empty_aligned(shape, dtype='float64', order="C", n=None)
    Function that returns an empty numpy array that is n-byte aligned,
    where ``n`` is determined by inspecting the CPU if it is not
    provided.
    The alignment is given by the final optional argument, ``n``. If
    ``n`` is not provided then this function will inspect the CPU to
    determine alignment. The rest of the arguments are as per
    :func:`numpy.empty`.
    """
    itemsize = np.dtype(dtype).itemsize

    # Apparently there is an issue with numpy.prod wrapping around on 32-bits
    # on Windows 64-bit. This shouldn't happen, but the following code
    # alleviates the problem.
    if not isinstance(shape, (int, np.integer)):
        array_length = 1
        for each_dimension in shape:
            array_length *= each_dimension

    else:
        array_length = shape

    base_ary = np.empty(array_length*itemsize+n, dtype=np.int8)

    # We now need to know how to offset base_ary
    # so it is correctly aligned
    _array_aligned_offset = (n-address_from_numpy(base_ary)) % n

    array = np.frombuffer(
            base_ary[_array_aligned_offset:_array_aligned_offset-n].data,
            dtype=dtype).reshape(shape, order=order)

    return array

# }}}


# {{{ pickled container value

class _PickledObject:
    """A class meant to wrap a pickled value (for :class:`LazilyUnpicklingDict` and
    :class:`LazilyUnpicklingList`).
    """

    def __init__(self, obj):
        if isinstance(obj, _PickledObject):
            self.objstring = obj.objstring
        else:
            from pickle import dumps
            self.objstring = dumps(obj)

    def unpickle(self):
        from pickle import loads
        return loads(self.objstring)

    def __getstate__(self):
        return {"objstring": self.objstring}

    def __repr__(self) -> str:
        return type(self).__name__ + "(" + repr(self.unpickle()) + ")"


class _PickledObjectWithEqAndPersistentHashKeys(_PickledObject):
    """Like :class:`_PickledObject`, with two additional attributes:

        * `eq_key`
        * `persistent_hash_key`

    This allows for comparison and for persistent hashing without unpickling.
    """

    def __init__(self, obj, eq_key, persistent_hash_key):
        _PickledObject.__init__(self, obj)
        self.eq_key = eq_key
        self.persistent_hash_key = persistent_hash_key

    def update_persistent_hash(self, key_hash, key_builder):
        key_builder.rec(key_hash, self.persistent_hash_key)

    def __getstate__(self):
        return {"objstring": self.objstring,
                "eq_key": self.eq_key,
                "persistent_hash_key": self.persistent_hash_key}

# }}}


# {{{ lazily unpickling dictionary

class LazilyUnpicklingDict(abc.MutableMapping):
    """A dictionary-like object which lazily unpickles its values.
    """

    def __init__(self, *args, **kwargs):
        self._map = dict(*args, **kwargs)

    def __getitem__(self, key):
        value = self._map[key]
        if isinstance(value, _PickledObject):
            value = self._map[key] = value.unpickle()
        return value

    def __setitem__(self, key, value):
        self._map[key] = value

    def __delitem__(self, key):
        del self._map[key]

    def __len__(self):
        return len(self._map)

    def __iter__(self):
        return iter(self._map)

    def __getstate__(self):
        return {"_map": {
            key: _PickledObject(val)
            for key, val in self._map.items()}}

    def __repr__(self) -> str:
        return type(self).__name__ + "(" + repr(self._map) + ")"

# }}}


# {{{ lazily unpickling list

class LazilyUnpicklingList(abc.MutableSequence):
    """A list which lazily unpickles its values."""

    def __init__(self, *args, **kwargs):
        self._list = list(*args, **kwargs)

    def __getitem__(self, key):
        item = self._list[key]
        if isinstance(item, _PickledObject):
            item = self._list[key] = item.unpickle()
        return item

    def __setitem__(self, key, value):
        self._list[key] = value

    def __delitem__(self, key):
        del self._list[key]

    def __len__(self):
        return len(self._list)

    def insert(self, key, value):
        self._list.insert(key, value)

    def __getstate__(self):
        return {"_list": [_PickledObject(val) for val in self._list]}

    def __add__(self, other):
        return self._list + other

    def __mul__(self, other):
        return self._list * other

    def __repr__(self) -> str:
        return type(self).__name__ + "(" + repr(self._list) + ")"


class LazilyUnpicklingListWithEqAndPersistentHashing(LazilyUnpicklingList):
    """A list which lazily unpickles its values, and supports equality comparison
    and persistent hashing without unpickling.

    Persistent hashing only works in conjunction with :class:`LoopyKeyBuilder`.

    Equality comparison and persistent hashing are implemented by supplying
    functions `eq_key_getter` and `persistent_hash_key_getter` to the
    constructor. These functions should return keys that can be used in place of
    the original object for the respective purposes of equality comparison and
    persistent hashing.
    """

    def __init__(self, *args, **kwargs):
        self.eq_key_getter = kwargs.pop("eq_key_getter")
        self.persistent_hash_key_getter = kwargs.pop("persistent_hash_key_getter")
        LazilyUnpicklingList.__init__(self, *args, **kwargs)

    def update_persistent_hash(self, key_hash, key_builder):
        key_builder.update_for_list(key_hash, self._list)

    def _get_eq_key(self, obj):
        if isinstance(obj, _PickledObjectWithEqAndPersistentHashKeys):
            return obj.eq_key
        return self.eq_key_getter(obj)

    def _get_persistent_hash_key(self, obj):
        if isinstance(obj, _PickledObjectWithEqAndPersistentHashKeys):
            return obj.persistent_hash_key
        return self.persistent_hash_key_getter(obj)

    def __eq__(self, other):
        if not isinstance(other, (list, LazilyUnpicklingList)):
            return NotImplemented

        if isinstance(other, LazilyUnpicklingList):
            other = other._list

        if len(self) != len(other):
            return False

        for a, b in zip(self._list, other):
            if self._get_eq_key(a) != self._get_eq_key(b):
                return False

        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __getstate__(self):
        return {"_list": [
                _PickledObjectWithEqAndPersistentHashKeys(
                    val,
                    self._get_eq_key(val),
                    self._get_persistent_hash_key(val))
                for val in self._list],
                "eq_key_getter": self.eq_key_getter,
                "persistent_hash_key_getter": self.persistent_hash_key_getter}

# }}}


# {{{ optional object

class _no_value:  # noqa
    pass


class Optional:
    """A wrapper for an optionally present object.

    .. attribute:: has_value

        *True* if and only if this object contains a value.

    .. attribute:: value

        The value, if present.
    """

    __slots__ = ("_value", "has_value")

    def __init__(self, value=_no_value):
        self.has_value = value is not _no_value
        if self.has_value:
            self._value = value

    def __str__(self):
        if not self.has_value:
            return "Optional()"
        return "Optional(%s)" % self._value

    def __repr__(self):
        if not self.has_value:
            return "Optional()"
        return "Optional(%r)" % self._value

    def __getstate__(self):
        if not self.has_value:
            return _no_value

        return (self._value,)

    def __setstate__(self, state):
        if state is _no_value:
            self.has_value = False
            return

        self.has_value = True
        self._value, = state

    def __eq__(self, other):
        if not self.has_value:
            return not other.has_value

        return self.value == other.value if other.has_value else False

    def __neq__(self, other):
        return not self.__eq__(other)

    @property
    def value(self):
        if not self.has_value:
            raise AttributeError("optional value not present")
        return self._value

    def update_persistent_hash(self, key_hash, key_builder):
        key_builder.rec(
                key_hash,
                (self._value,) if self.has_value else ())

    def __hash__(self):
        if not self.has_value:
            return hash((type(self), False))
        else:
            return hash((self.has_value, self._value))

# }}}


def unpickles_equally(obj):
    from pickle import dumps, loads
    return loads(dumps(obj)) == obj


def is_interned(s):
    return s is None or intern(s) is s


def intern_frozenset_of_ids(fs):
    return frozenset(intern(s) for s in fs)


# {{{ t_unit_to_python

def _is_generated_t_unit_the_same(python_code, var_name, ref_t_unit):
    """
    Helper for :func:`kernel_to_python`. Returns *True* only if the variable
    referenced by *var_name* in *python_code* is equal to *kernel*, else
    returns *False*.
    """
    reproducer_variables = {}
    exec(python_code, reproducer_variables)
    t_unit = reproducer_variables[var_name]
    return ref_t_unit == t_unit


# {{{ CallablesUnresolver

class _CallablesUnresolver(RuleAwareIdentityMapper):
    def __init__(self, rule_mapping_context, callables_table, target):
        super().__init__(rule_mapping_context)
        self.callables_table = callables_table
        self.target = target

    @cached_property
    def known_callables(self):
        from loopy.kernel.function_interface import CallableKernel
        return (frozenset(self.target.get_device_ast_builder().known_callables)
                | {name
                   for name, clbl in self.callables_table.items()
                   if isinstance(clbl, CallableKernel)})

    def map_call(self, expr, expn_state):
        from loopy.symbolic import ResolvedFunction
        if isinstance(expr.function, ResolvedFunction):
            if expr.function.name not in self.known_callables:
                raise NotImplementedError("User-provided scalar callables not"
                                          " supported yet.")

            from pymbolic.primitives import Call
            return Call(expr.function.function, tuple(self.rec(par, expn_state)
                                                      for par in expr.parameters))
        else:
            return super().map_call(expr, expn_state)


def _unresolve_callables(kernel, callables_table):
    from loopy.kernel import KernelState
    from loopy.symbolic import SubstitutionRuleMappingContext

    vng = kernel.get_var_name_generator()
    rule_mapping_context = SubstitutionRuleMappingContext(kernel.substitutions,
                                                          vng)
    mapper = _CallablesUnresolver(rule_mapping_context,
                                  callables_table,
                                  kernel.target)
    return (rule_mapping_context.finish_kernel(mapper.map_kernel(kernel))
            .copy(state=KernelState.INITIAL))

# }}}


def _kernel_to_python(kernel, is_entrypoint=False, var_name="kernel"):
    from mako.template import Template

    from loopy.kernel.instruction import BarrierInstruction, MultiAssignmentBase

    options = {}  # options: mapping from insn_id to str of options

    for insn in kernel.instructions:
        option = f"id={insn.id}, "
        if insn.depends_on:
            option += ("dep="+":".join(insn.depends_on)+", ")
        if insn.tags:
            option += ("tags="+":".join(insn.tags)+", ")
        if insn.within_inames is not None:
            if insn.within_inames_is_final:
                option += ("inames="+":".join(insn.within_inames)+", ")
            else:
                option += ("inames=+"+":".join(insn.within_inames)+", ")

        if isinstance(insn, MultiAssignmentBase):
            if insn.atomicity:
                option += "atomic, "
        elif isinstance(insn, BarrierInstruction):
            option += (f"mem_kind={insn.mem_kind}, ")
        else:
            pass

        options[insn.id] = option[:-2]  # get rid of the trailing ", "

    make_kernel = "make_kernel" if is_entrypoint else "make_function"

    python_code = r"""
    <%! import loopy as lp %>

    <%! tv_aspace = {0: 'lp.AddressSpace.PRIVATE', 1: 'lp.AddressSpace.LOCAL',
    2: 'lp.AddressSpace.GLOBAL', lp.auto: 'lp.auto' } %>
    ${var_name} = lp.${make_kernel}(
        [
        % for dom in kernel.domains:
        "${str(dom)}",
        % endfor
        ],
        '''
        % for name, rule in sorted(kernel.substitutions.items(), key=lambda x: x[0]):
        ${name}(${", ".join(rule.arguments)}) := ${str(rule.expression)}
        %endfor

        % for id, opts in options.items():
        <% insn = kernel.id_to_insn[id] %>
        % if isinstance(insn, lp.MultiAssignmentBase):
        ${','.join([str(a) for a in insn.assignees])} = ${insn.expression} {${opts}}
        % elif isinstance(insn, lp.BarrierInstruction):
        ... ${insn.synchronization_kind[0]}barrier {${opts}}
        % elif isinstance(insn, lp.NoOpInstruction):
        ... nop {${opts}}
        % else:
        <% raise NotImplementedError(f"Not implemented for {type(insn)}.")%>
        % endif
        %endfor
        ''', [
            % for arg in kernel.args:
            % if isinstance(arg, lp.ValueArg):
            lp.ValueArg(
                name="${arg.name}",
                dtype=${('np.'+arg.dtype.numpy_dtype.name
                            if arg.dtype else 'None')}),
            % else:
            lp.GlobalArg(
                name="${arg.name}", dtype=${('np.'+arg.dtype.numpy_dtype.name
                                                if arg.dtype else 'None')},
                shape=${arg.shape}, for_atomic=${arg.for_atomic}),
            % endif
            % endfor
            % for tv in kernel.temporary_variables.values():
            lp.TemporaryVariable(
                name="${tv.name}",
                dtype=${'np.'+tv.dtype.numpy_dtype.name if tv.dtype else 'lp.auto'},
                shape=${tv.shape}, for_atomic=${tv.for_atomic},
                address_space=${tv_aspace[tv.address_space]},
                read_only=${tv.read_only},
                % if tv.initializer is not None:
                initializer=${"np."+repr(tv.initializer)},
                % endif
                ),
            % endfor
            ],
            lang_version=${lp.MOST_RECENT_LANGUAGE_VERSION},
    % if kernel.iname_slab_increments:
            iname_slab_increments=${repr(kernel.iname_slab_increments)},
    % endif
    % if kernel.applied_iname_rewrites:
            applied_iname_rewrites=${repr(kernel.applied_iname_rewrites)},
    % endif
    % if kernel.name != "loopy_kernel":
            name="${kernel.name}",
    % endif
            )

    % for iname in kernel.inames.values():
    % for tag in iname.tags:
    ${var_name} = lp.tag_inames(${var_name}, "${"%s:%s" %(iname.name, tag)}")
    % endfor

    % endfor
    """

    python_code = Template(python_code,
                           strict_undefined=True).render(options=options,
                                                         kernel=kernel,
                                                         make_kernel=make_kernel,
                                                         var_name=var_name)
    python_code = remove_lines_with_only_spaces(
            remove_common_indentation(python_code))

    return python_code


def t_unit_to_python(t_unit, var_name="t_unit",
                     return_preamble_and_body_separately=False):
    """"
    Returns a :class:`str` of a python code that instantiates *kernel*.

    :arg kernel: An instance of :class:`loopy.LoopKernel`
    :arg var_name: A :class:`str` of the kernel variable name in the generated
        python script.
    :arg return_preamble_and_body_separately: A :class:`bool`.
        If *True* returns ``(preamble, body)``, where ``preamble`` includes the
        import statements and ``body`` includes the kernel, translation unit
        instantiation code.

    .. note::

        The implementation is partially complete and a :class:`AssertionError`
        is raised if the returned python script does not exactly reproduce
        *kernel*. Contributions are welcome to fill in the missing voids.
    """
    from loopy.kernel.function_interface import CallableKernel

    new_callables = {name: CallableKernel(_unresolve_callables(clbl.subkernel,
                                                               t_unit
                                                               .callables_table))
                     for name, clbl in t_unit.callables_table.items()
                     if isinstance(clbl, CallableKernel)}
    t_unit = t_unit.copy(callables_table=Map(new_callables))

    knl_python_code_srcs = [_kernel_to_python(clbl.subkernel,
                                              name in t_unit.entrypoints,
                                              f"{name}_knl"
                                              )
                            for name, clbl in t_unit.callables_table.items()]

    knl_args = ", ".join(f"{name}_knl" for name in t_unit.callables_table)
    merge_stmt = f"{var_name} = lp.merge([{knl_args}])"

    preamble_str = "\n".join([
        "import loopy as lp",
        "import numpy as np",
        "from pymbolic.primitives import *",
        "import immutables",
        ])
    body_str = "\n".join([*knl_python_code_srcs, "\n", merge_stmt])

    python_code = "\n".join([preamble_str, "\n", body_str])
    assert _is_generated_t_unit_the_same(python_code, var_name, t_unit)

    if return_preamble_and_body_separately:
        return preamble_str, body_str
    else:
        return python_code

# }}}


# {{{ cache management

caches: List[WriteOncePersistentDict] = []


def clear_in_mem_caches() -> None:
    for cache in caches:
        cache.clear_in_mem_cache()

# }}}


# {{{ memoize_on_disk

def memoize_on_disk(func, key_builder_t=LoopyKeyBuilder):
    from functools import wraps

    from pytools.persistent_dict import WriteOncePersistentDict

    from loopy.kernel import LoopKernel
    from loopy.translation_unit import TranslationUnit
    from loopy.version import DATA_MODEL_VERSION

    transform_cache = WriteOncePersistentDict(
        ("loopy-memoize-cache-"
            f"{func.__name__}-"
            f"{key_builder_t.__qualname__}.{key_builder_t.__name__}"
            f"-v0-{DATA_MODEL_VERSION}"),
        key_builder=key_builder_t(),
        safe_sync=False)

    caches.append(transform_cache)

    @wraps(func)
    def wrapper(*args, **kwargs):
        from loopy import CACHING_ENABLED

        if (not CACHING_ENABLED
                or kwargs.pop("_no_memoize_on_disk", False)):
            return func(*args, **kwargs)

        cache_key = (func.__qualname__, func.__name__, args, kwargs)

        try:
            result = transform_cache[cache_key]
            logger.debug(f"Function {func.__name__} returned from"
                         " memoized result on disk.")
            return result
        except KeyError:
            logger.debug(f"Function {func.__name__} not present"
                         " on disk.")
            if args and isinstance(args[0], LoopKernel):
                proc_log_str = f"{func.__name__} on '{args[0].name}'"
            elif args and isinstance(args[0], TranslationUnit):
                entrypoints_str = ", ".join(args[0].entrypoints)
                proc_log_str = f"{func.__name__} on '{entrypoints_str}'"
            else:
                proc_log_str = f"{func.__name__}"

            with ProcessLogger(logger, proc_log_str):
                result = func(*args, **kwargs)

            transform_cache.store_if_not_present(cache_key, result)
            return result

    return wrapper

# }}}


def is_hashable(o: object) -> bool:
    try:
        hash(o)
    except TypeError:
        return False
    return True


# {{{ tree data structure

T = TypeVar("T")


@dataclass(frozen=True)
class Tree(Generic[T]):
    """
    An immutable tree implementation.

    .. automethod:: ancestors
    .. automethod:: parent
    .. automethod:: children
    .. automethod:: depth
    .. automethod:: rename_node
    .. automethod:: move_node

    .. note::

       Almost all the operations are implemented recursively. NOT suitable for
       deep trees. At the very least if the Python implementation is CPython
       this allocates a new stack frame for each iteration of the operation.
    """
    _parent_to_children: "PMap[T, FrozenSet[T]]"
    _child_to_parent: "PMap[T, Optional[T]]"

    @staticmethod
    def from_root(root: T):
        return Tree(pmap({root: frozenset()}),
                    pmap({root: None}))

    @cached_property
    def root(self) -> T:
        guess = set(self._child_to_parent).pop()
        while self.parent(guess) is not None:
            guess = self.parent(guess)

        return guess

    @memoize_method
    def ancestors(self, node: T) -> "FrozenSet[T]":
        """
        Returns a :class:`frozenset` of nodes that are ancestors of *node*.
        """
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        if self.is_root(node):
            # => root
            return frozenset()

        parent = self._child_to_parent[node]

        return frozenset([parent]) | self.ancestors(parent)

    def parent(self, node: T) -> "Optional[T]":
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        return self._child_to_parent[node]

    def children(self, node: T) -> "FrozenSet[T]":
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        return self._parent_to_children[node]

    @memoize_method
    def depth(self, node: T) -> int:
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        if self.is_root(node):
            # => None
            return 0

        return 1 + self.depth(self.parent(node))

    def is_root(self, node: T) -> bool:
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        return self.parent(node) is None

    def is_leaf(self, node: T) -> bool:
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not in tree.")

        return len(self.children(node)) == 0

    def is_a_node(self, node: T) -> bool:
        return node in self._child_to_parent

    def add_node(self, node: T, parent: T) -> "Tree[T]":
        """
        Returns a :class:`Tree` with added node *node* having a parent
        *parent*.
        """
        if self.is_a_node(node):
            raise ValueError(f"'{node}' already present in tree.")

        siblings = self._parent_to_children[parent]

        return Tree((self._parent_to_children
                     .set(parent, siblings | frozenset([node]))
                     .set(node, frozenset())),
                    self._child_to_parent.set(node, parent))

    def rename_node(self, node: T, new_id: T) -> "Tree[T]":
        """
        Returns a copy of *self* with *node* renamed to *new_id*.
        """
        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not present in tree.")

        if self.is_a_node(new_id):
            raise ValueError(f"cannot rename to '{new_id}', as its already a part"
                             " of the tree.")

        parent = self.parent(node)
        children = self.children(node)

        # {{{ update child to parent

        new_child_to_parent = (self._child_to_parent.discard(node)
                               .set(new_id, parent))

        for child in children:
            new_child_to_parent = (new_child_to_parent
                                   .set(child, new_id))

        # }}}

        # {{{ update parent_to_children

        new_parent_to_children = (self._parent_to_children
                                  .discard(node)
                                  .set(new_id, self.children(node)))

        if parent is not None:
            # update the child's name in the parent's children
            new_parent_to_children = (new_parent_to_children
                                      .discard(parent)
                                      .set(parent, ((self.children(parent)
                                                    - frozenset([node]))
                                                    | frozenset([new_id]))))

        # }}}

        return Tree(new_parent_to_children,
                    new_child_to_parent)

    def move_node(self, node: T, new_parent: "Optional[T]") -> "Tree[T]":
        """
        Returns a copy of *self* with node *node* as a child of *new_parent*.
        """
        if self.is_root(node) and new_parent is not None:
            raise ValueError("Moving root not allowed.")

        if not self.is_a_node(node):
            raise ValueError(f"'{node}' not a part of the tree => cannot move.")

        if not self.is_a_node(new_parent):
            raise ValueError(f"Cannot move to '{new_parent}' as it's not in tree.")

        parent = self.parent(node)
        siblings = self.children(parent)
        parents_new_children = siblings - frozenset([node])
        new_parents_children = self.children(new_parent) | frozenset([node])

        new_child_to_parent = self._child_to_parent.set(node, new_parent)
        new_parent_to_children = (self._parent_to_children
                                  .set(parent, parents_new_children)
                                  .set(new_parent, new_parents_children))

        return Tree(new_parent_to_children,
                    new_child_to_parent)

    def __str__(self):
        """
        Stringifies the tree by using the box-drawing unicode characters.

        ::

            >>> from loopy.tools import Tree
            >>> tree = (Tree.from_root("Root")
            ...         .add_node("A", "Root")
            ...         .add_node("B", "Root")
            ...         .add_node("D", "B")
            ...         .add_node("E", "B")
            ...         .add_node("C", "A"))

            >>> print(tree)
            Root
            ├── A
            │   └── C
            └── B
                ├── D
                └── E
        """
        def rec(node):
            children_result = [rec(c) for c in self.children(node)]

            def post_process_non_last_child(child):
                return ["├── " + child[0]] + [f"│   {c}" for c in child[1:]]

            def post_process_last_child(child):
                return ["└── " + child[0]] + [f"    {c}" for c in child[1:]]

            children_result = ([post_process_non_last_child(c)
                                for c in children_result[:-1]]
                            + [post_process_last_child(c)
                                for c in children_result[-1:]])
            return [str(node)] + sum(children_result, start=[])

        return "\n".join(rec(self.root))

    def nodes(self) -> "Iterator[T]":
        return iter(self._child_to_parent.keys())

# }}}

# vim: fdm=marker
