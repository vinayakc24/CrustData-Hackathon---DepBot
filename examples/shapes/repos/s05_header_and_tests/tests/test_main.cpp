#include <mylib/api.hpp>

int main() {
    return mylib::add(2, 3) == 5 && toydep::old_sum(1, 1) == 2 ? 0 : 1;
}
