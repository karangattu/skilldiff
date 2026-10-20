from shiny import App, reactive, render, ui


def compute_metrics(n: int) -> list[int]:
    return [i * i for i in range(max(1, n))]

app_ui = ui.page_fluid(
    ui.input_numeric("n", "N", value=100),
    ui.output_text("total_out"),
    ui.output_text("average_out"),
)

def server(input, output, session):
    @reactive.calc
    def metrics():
        return compute_metrics(input.n())

    @render.text
    def total_out():
        vals = metrics()
        return f"Total: {sum(vals)}"

    @render.text
    def average_out():
        vals = metrics()
        return f"Average: {sum(vals) / len(vals):.2f}"

app = App(app_ui, server)
