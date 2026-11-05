#include <toydep/toydep.hpp>
namespace mylib { int old_sum(int a, int b) { return a - b; } }  // same-name trap
// old_sum is mentioned in a comment
const char* label = "old_sum";
int direct() { return toydep::old_sum(1, 2); }
int other() { return mylib::old_sum(3, 1); }
using toydep::old_sum;
int via_using() { return old_sum(4, 5); }
#define ADD(a, b) toydep::old_sum(a, b)
int via_macro() { return ADD(1, 1); }
template <class T> T tmpl(T a) { return toydep::old_sum(a, a); }
int inst() { return tmpl(3); }
auto fp = &toydep::old_sum;
#ifdef _WIN32
int windows_only() { return toydep::old_sum(9, 9); }
#endif
int unrelated() { return toydep::twice(2); }
