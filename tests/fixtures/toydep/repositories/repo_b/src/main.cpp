#include <toydep/toydep.hpp>

int main() {
    const int total = toydep::old_sum(-4, 9);
    return total == 5 ? 0 : 1;
}
