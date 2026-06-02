
// Fonction pour calculer le n-ième nombre de Fibonacci
function fibonacci(n) {
  if (n <= 1) {
    return n;
  }
  let a = 0;
  let b = 1;
  for (let i = 2; i <= n; i++) {
    let temp = a + b;
    a = b;
    b = temp;
  }
  return b;
}

// Calculer et afficher les 10 premiers nombres de Fibonacci
console.log("Les 10 premiers nombres de Fibonacci sont :");
for (let i = 0; i < 10; i++) {
  console.log(`Fibonacci(${i}) = ${fibonacci(i)}`);
}
