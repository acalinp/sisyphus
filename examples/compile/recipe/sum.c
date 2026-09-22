#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    printf("%ld\n", strtol(argv[1], NULL, 10) - strtol(argv[2], NULL, 10));
    return 0;
}
