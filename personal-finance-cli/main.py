from finance import FinanceManager

def menu():
    print("\n=== Controle Financeiro ===")
    print("1 - Adicionar Receita")
    print("2 - Adicionar Despesa")
    print("3 - Listar Transações")
    print("4 - Mostrar Saldo")
    print("0 - Sair")

def main():
    manager = FinanceManager()

    while True:
        menu()
        choice = input("Escolha uma opção: ")

        if choice == "1":
            description = input("Descrição: ")
            amount = float(input("Valor: "))
            manager.add_transaction(description, amount, "receita")

        elif choice == "2":
            description = input("Descrição: ")
            amount = float(input("Valor: "))
            manager.add_transaction(description, amount, "despesa")

        elif choice == "3":
            manager.list_transactions()

        elif choice == "4":
            manager.show_balance()

        elif choice == "0":
            print("Encerrando...")
            break

        else:
            print("Opção inválida.")

if __name__ == "__main__":
    main()
