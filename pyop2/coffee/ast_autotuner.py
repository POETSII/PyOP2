# This file is part of PyOP2
#
# PyOP2 is Copyright (c) 2012, Imperial College London and
# others. Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

"""COFFEE's autotuning system."""

import pyop2.compilation as compilation
import ctypes

from ast_base import *
from ast_vectorizer import vect_roundup


class Autotuner(object):

    _code_template = """
// This file was automatically generated by COFFEE for kernels autotuning.

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

// Timing
#include <stdint.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

// Firedrake headers
#include "firedrake_geometry.h"

%(vect_header)s
#define VECTOR_ALIGN %(vect_align)d
%(blas_header)s
%(blas_namespace)s

#define RESOLUTION %(resolution)d
#define TOLERANCE 0.000000000001

long stamp()
{
  struct timespec tv;
  clock_gettime(CLOCK_MONOTONIC, &tv);
  return tv.tv_sec * 1000 * 1000 * 1000 + tv.tv_nsec;
}

#ifdef DEBUG
static int compare_1d(double A1[%(trial)s], double A2[%(trial)s])
{
  for(int i = 0; i < %(trial)s; i++)
  {
    if(fabs(A1[i] - A2[i]) > TOLERANCE)
    {
      return 1;
    }
  }
  return 0;
}

static int compare_2d(double A1[%(test)s][%(test)s], double A2[%(test)s][%(test)s])
{
  for(int i = 0; i < %(trial)s; i++)
  {
    for(int j = 0; j < %(test)s; j++)
    {
      if(fabs(A1[i][j] - A2[i][j]) > TOLERANCE)
      {
        printf("i=%%d, j=%%d, A1[i][j]=%%f, A2[i][j]=%%f\\n", i, j, A1[i][j], A2[i][j]);
        return 1;
      }
    }
  }
  return 0;
}
#endif

%(globals)s

%(variants)s

%(externc_open)s
int autotune()
{
  int i = 0, c = 0;
  int counters[%(nvariants)d] = {0};

  /* Call kernel variants */
  %(call_variants)s

  /* Find the fastest variant */
  int best = 0;
  for(int j = 0; j < %(nvariants)d; j++)
  {
    if(counters[j] > counters[best])
    {
      best = j;
    }
  }

  /* Output fastest variant */
  /*
  printf("COFFEE Autotuner: cost of variants:\\n");
  for (int j = 0; j < %(nvariants)d; j++)
  {
    printf("  Variant %%d: %%d\\n", j, counters[j]);
  }
  printf("COFFEE Autotuner: fastest variant has ID %%d\\n", best);
  */

#ifdef DEBUG
  %(debug_code)s
#endif

  return best;
}
%(externc_close)s
"""
    _coeffs_template = """
    // Initialize coefficients
    for (int j = 0; j < %(ndofs)d; j++)
    {
      %(init_coeffs)s
    }
"""
    _run_template = """
  // Code variant %(iter)d call
  srand (1);
  long start%(iter)d, end%(iter)d;
  %(decl_params)s
  start%(iter)d = stamp();
  end%(iter)d = start%(iter)d + RESOLUTION;
  while (stamp() < end%(iter)d)
  {
    // Initialize coordinates
    for (int j = 0; j < %(ncoords)d; j++)
    {
      vertex_coordinates_%(iter)d[j][0] = (double)rand();
    }
    %(init_coeffs)s
    %(call_variant)s
    c++;
  }
  counters[i++] = c;
  c = 0;
"""
    _debug_template = """
  if(%(call_debug)s(A_0, A_%(iter)s))
  {
    printf("COFFEE Warning: code variants 0 and %%d differ\\n", %(iter)s);
  }
"""
    _filename = "autotuning_code."
    _coord_size = {
        'compute_jacobian_interval_1d': 2,
        'compute_jacobian_interval_2d': 4,
        'compute_jacobian_interval_3d': 6,
        'compute_jacobian_quad_2d': 8,
        'compute_jacobian_quad_3d': 12,
        'compute_jacobian_triangle_2d': 6,
        'compute_jacobian_triangle_3d': 9,
        'compute_jacobian_tetrahedron_3d': 12,
        'compute_jacobian_prism_3d': 18,
        'compute_jacobian_interval_int_1d': 4,
        'compute_jacobian_interval_int_2d': 8,
        'compute_jacobian_quad_int_2d': 16,
        'compute_jacobian_quad_int_3d': 24,
        'compute_jacobian_interval_int_3d': 12,
        'compute_jacobian_triangle_int_2d': 12,
        'compute_jacobian_triangle_int_3d': 18,
        'compute_jacobian_tetrahedron_int_3d': 24,
        'compute_jacobian_prism_int_3d': 36
    }

    """Create and execute a C file in which multiple variants of the same kernel
    are executed to determine the fastest implementation."""

    def __init__(self, kernels, itspace, include_dirs, compiler, isa, blas):
        """Initialize the autotuner.

        :arg kernels:      list of code snippets implementing the kernel.
        :arg itspace:      kernel's iteration space.
        :arg include_dirs: list of directories to be searched for header files
        :arg compiler:     backend compiler info
        :arg isa:          instruction set architecture info
        :arg blas:         COFFEE's dense linear algebra library info
        """

        self.kernels = kernels
        self.itspace = itspace
        self.include_dirs = include_dirs
        self.compiler = compiler
        self.isa = isa
        self.blas = blas

    def _retrieve_coords_size(self, kernel):
        """Return coordinates array size"""
        for i in Autotuner._coord_size:
            if i in kernel:
                return Autotuner._coord_size[i]
        raise RuntimeError("COFFEE: Autotuner does not know how to expand the jacobian")

    def _retrieve_coeff_size(self, root, coeffs):
        """Return coefficient sizes, rounded up to multiple of vector length"""
        def find_coeff_size(node, coeff, loop_sizes):
            if isinstance(node, FlatBlock):
                return 0
            elif isinstance(node, Symbol):
                if node.symbol == coeff:
                    return loop_sizes[node.rank[0]] if node.rank[0] != '0' else 1
                return 0
            elif isinstance(node, For):
                loop_sizes[node.it_var()] = node.size()
            for n in node.children:
                size = find_coeff_size(n, coeff, loop_sizes)
                if size:
                    return size

        coeffs_size = {}
        for c in coeffs:
            size = find_coeff_size(root, c, {})
            coeffs_size[c] = vect_roundup(size if size else 1)  # Else handles constants case
        return coeffs_size

    def _run(self, src):
        """Compile and run the generated test cases. Return the fastest kernel version."""

        filetype = "c"
        cppargs = ["-std=gnu99"] + ["-I%s" % d for d in self.include_dirs]
        ldargs = ["-lrt", "-lm"]
        if self.compiler:
            cppargs += [self.compiler[self.isa['inst_set']]]
        if self.blas:
            blas_dir = self.blas['dir']
            if blas_dir:
                cppargs += ["-I%s/include" % blas_dir]
                ldargs += ["-L%s/lib" % blas_dir]
            ldargs += self.blas['link']
            if self.blas['name'] == 'eigen':
                filetype = "cpp"

        # Dump autotuning src out to a file
        with open(Autotuner._filename + filetype, 'w') as f:
            f.write(src)

        return compilation.load(src, filetype, "autotune", cppargs, ldargs, None,
                                ctypes.c_int, self.compiler.get('name'))()

    def tune(self, resolution):
        """Return the fastest kernel implementation.

        :arg resolution: the amount of time in milliseconds a kernel is run."""

        is_global_decl = lambda s: isinstance(s, Decl) and ('static' and 'const' in s.qual)
        coords_size = self._retrieve_coords_size(str(self.kernels[0]))
        trial_dofs = self.itspace[0][0].size() if len(self.itspace) >= 1 else 0
        test_dofs = self.itspace[1][0].size() if len(self.itspace) >= 2 else 0
        coeffs_size = {}

        # Create the invidual test cases
        variants, debug_code, global_decls = ([], [], [])
        for ast, i in zip(self.kernels, range(len(self.kernels))):
            fun_decl = ast.children[1]
            # Create ficticious kernel parameters
            # Here, we follow the "standard" convention:
            # - The first parameter is the local tensor (lt)
            # - The second parameter is the coordinates field (coords)
            # - (Optional) any additional parameter is a generic field,
            #   whose size is bound to the number of dofs in the kernel
            lt_arg = fun_decl.args[0].sym
            lt_sym = lt_arg.symbol + "_%d" % i
            coords_sym = fun_decl.args[1].sym.symbol.replace('*', '')
            coeffs_syms = [f.sym.symbol.replace('*', '') for f in fun_decl.args[2:]]
            coeffs_types = [f.typ for f in fun_decl.args[2:]]
            lt_init = "".join("{" for r in lt_arg.rank) + "0.0" + "".join("}" for r in lt_arg.rank)
            lt_decl = "double " + lt_sym + "".join(["[%d]" % r for r in lt_arg.rank]) + \
                self.compiler['align']("VECTOR_ALIGN") + " = " + lt_init
            coords_decl = "double " + coords_sym + "_%d[%d][1]" % (i, coords_size)
            coeffs_size = coeffs_size or self._retrieve_coeff_size(fun_decl, coeffs_syms)
            coeffs_decl = ["%s " % t + f + "_%d[%d][1]" % (i, coeffs_size[f]) for t, f
                           in zip(coeffs_types, coeffs_syms)]
            # Adjust kernel's signature
            fun_decl.args[1].sym = Symbol(coords_sym, ("%d" % coords_size, 1))
            for d, f in zip(fun_decl.args[2:], coeffs_syms):
                d.sym = Symbol(f, ("%d" % coeffs_size[f], 1))
            # Adjust symbols names for kernel invokation
            coords_sym += "_%d" % i
            coeffs_syms = [f + "_%d" % i for f in coeffs_syms]

            # Adjust kernel name
            fun_decl.name = fun_decl.name + "_%d" % i

            # Remove any static const declaration from the kernel (they are declared
            # just once at the beginning of the file, to reduce code size)
            fun_body = fun_decl.children[0].children
            global_decls = global_decls or "\n".join([str(s) for s in fun_body if is_global_decl(s)])
            fun_decl.children[0].children = [s for s in fun_body if not is_global_decl(s)]

            # Initialize coefficients (if any)
            init_coeffs = ""
            if coeffs_syms:
                init_coeffs = Autotuner._coeffs_template % {
                    'ndofs': min(coeffs_size.values()),
                    'init_coeffs': ";\n      ".join([f + "[j][0] = (double)rand();" for f in coeffs_syms])
                }

            # Instantiate code variant
            params = ", ".join([lt_sym, coords_sym] + coeffs_syms)
            variants.append(Autotuner._run_template % {
                'iter': i,
                'decl_params': ";\n  ".join([lt_decl, coords_decl] + coeffs_decl) + ";",
                'ncoords': coords_size,
                'init_coeffs': init_coeffs,
                'call_variant': fun_decl.name + "(%s);" % params
            })

            # Create debug code
            debug_code.append(Autotuner._debug_template % {
                'iter': i,
                'call_debug': "compare_2d" if trial_dofs and test_dofs else "compare_1d"
            })

        # Instantiate the autotuner skeleton
        kernels_code = "\n".join(["/* Code variant %d */" % i + str(k.children[1]) for i, k
                                  in zip(range(len(self.kernels)), self.kernels)])
        code_template = Autotuner._code_template % {
            'trial': trial_dofs,
            'test': test_dofs,
            'vect_header': self.compiler['vect_header'],
            'vect_align': self.isa['alignment'],
            'blas_header': self.blas['header'],
            'blas_namespace': self.blas['namespace'],
            'resolution': resolution,
            'globals': global_decls,
            'variants': kernels_code,
            'nvariants': len(self.kernels),
            'call_variants': "".join(variants),
            'externc_open': 'extern "C" {' if self.blas.get('name') in ['eigen'] else "",
            'externc_close': "}" if self.blas.get('name') in ['eigen'] else "",
            'debug_code': "".join(debug_code)
        }

        # Clean code from spurious pragmas
        code_template = '\n'.join(l for l in code_template.split("\n")
                                  if not l.strip().startswith('#pragma pyop2'))

        return self._run(code_template)
