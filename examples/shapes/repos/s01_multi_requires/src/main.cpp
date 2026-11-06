#include <otherdep/otherdep.hpp>
#include <toydep/toydep.hpp>

int main() {
    return toydep::old_sum(otherdep::two(), 3) == 5 ? 0 : 1;
}
