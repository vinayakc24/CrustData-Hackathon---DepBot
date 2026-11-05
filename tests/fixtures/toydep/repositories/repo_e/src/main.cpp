#include <toydep/toydep.hpp>

// Builds the function name by token pasting: `old_sum` never appears literally.
#define TOYDEP_OLD(name) toydep::old_##name

int main() {
    return toydep::old_sum(2, 3) == 5 && TOYDEP_OLD(sum)(1, 1) == 2 ? 0 : 1;
}
