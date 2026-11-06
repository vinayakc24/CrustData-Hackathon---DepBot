#include <toydep/toydep.hpp>
#include <cstring>

// We used to call toydep::old_sum(a, b) here.
int main() {
    const char* note = "toydep::old_sum(1, 2) is deprecated";
    return std::strlen(note) > 0 ? 0 : 1;
}
