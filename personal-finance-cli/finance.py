from storage import load_data, save_data

class FinanceManager:

    def __init__(self):
        self.transactions = load_data()

    def add_transaction(self, description, amount, t_type):
        if t_type == "despesa":
            amount = -abs(amount)

        transaction = {
            "description": description,
            "amount": amount
        }

        self.transactions.append(transaction)
        save_data(self.transactions)
        print("Transação adicionada com sucesso!")

    def list_transactions(self):
        if not self.transactions:
            print("Nenhuma transação encontrada.")
            return

        for t in self.transactions:
            print(f"{t['description']} | R$ {t['amount']}")

    def show_balance(self):
        balance = sum(t["amount"] for t in self.transactions)
        print(f"Saldo atual: R$ {balance}")
