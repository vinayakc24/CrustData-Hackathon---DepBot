#include <toydep/toydep.hpp>

#include "calls.inc"

int main() {
    return toydep::old_sum(2, 3) == 5 && via_inc() == 2 ? 0 : 1;
}
