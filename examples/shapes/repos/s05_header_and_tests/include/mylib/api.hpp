#pragma once
#include <toydep/toydep.hpp>

namespace mylib {
inline int add(int a, int b) { return toydep::old_sum(a, b); }
}
