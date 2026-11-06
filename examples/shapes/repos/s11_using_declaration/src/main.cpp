#include <toydep/toydep.hpp>

using toydep::old_sum;

int main() {
    return old_sum(2, 3) == 5 ? 0 : 1;
}
